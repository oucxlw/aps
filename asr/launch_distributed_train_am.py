#!/usr/bin/env python

# wujian@2019

import yaml
import codecs
import random
import pprint
import pathlib
import argparse

import torch as th
import numpy as np

from libs.utils import StrToBoolAction
from libs.distributed_trainer import S2STrainer

from loader import support_loader
from feats import support_transform
from nn import support_nnet

constrained_conf_keys = [
    "nnet_type", "nnet_conf", "data_conf", "trainer_conf", "asr_transform",
    "enh_transform"
]


def train_worker(rank, nnet, conf, args):
    """
    Initalize training workers
    """
    # construct trainer
    # torch.distributed.launch will provide
    # environment variables, and requires that you use init_method="env://".
    trainer = S2STrainer(rank,
                         nnet,
                         cuda_devices=args.num_process,
                         checkpoint=args.checkpoint,
                         resume=args.resume,
                         save_interval=args.save_interval,
                         prog_interval=args.prog_interval,
                         tensorboard=args.tensorboard,
                         **conf["trainer_conf"])

    data_conf = conf["data_conf"]
    trn_loader = support_loader(**data_conf["train"],
                                train=True,
                                distributed=True,
                                fmt=data_conf["fmt"],
                                batch_size=args.batch_size,
                                **data_conf["loader"])
    dev_loader = support_loader(**data_conf["valid"],
                                train=False,
                                fmt=data_conf["fmt"],
                                batch_size=args.batch_size,
                                **data_conf["loader"])

    print(f"Rank {rank}: Number of batches (train/valid) = " +
          f"{len(trn_loader)}/{len(dev_loader)}",
          flush=True)
    if args.eval_interval > 0:
        trainer.run_batch_per_epoch(trn_loader,
                                    dev_loader,
                                    num_epoches=args.epoches,
                                    eval_interval=args.eval_interval)
    else:
        trainer.run(trn_loader, dev_loader, num_epoches=args.epoches)


def run(args):
    # set random seed
    random.seed(args.seed)
    np.random.seed(args.seed)
    th.random.manual_seed(args.seed)

    if th.cuda.device_count() < args.num_process:
        raise RuntimeError("--num-process exceeds number of the GPUs")

    # new logger instance
    print("Arguments in args:\n{}".format(pprint.pformat(vars(args))),
          flush=True)

    checkpoint = pathlib.Path(args.checkpoint)
    checkpoint.mkdir(exist_ok=True, parents=True)
    # if exist, resume training
    last_checkpoint = checkpoint / "last.pt.tar"
    resume = args.resume
    if last_checkpoint.exists():
        args.resume = last_checkpoint.as_posix()

    # load configurations
    with open(args.conf, "r") as f:
        conf = yaml.full_load(f)

    # add dictionary info
    with codecs.open(args.dict, encoding="utf-8") as f:
        vocab = {}
        for line in f:
            unit, idx = line.split()
            vocab[unit] = int(idx)

    conf["nnet_conf"]["sos"] = vocab["<sos>"]
    conf["nnet_conf"]["eos"] = vocab["<eos>"]
    conf["nnet_conf"]["vocab_size"] = len(vocab)

    if "nnet_type" not in conf:
        conf["nnet_type"] = "las"
    for key in conf.keys():
        if key not in constrained_conf_keys:
            raise ValueError(f"Invalid configuration item: {key}")

    print("Arguments in yaml:\n{}".format(pprint.pformat(conf)), flush=True)

    asr_cls = support_nnet(conf["nnet_type"])
    asr_transform = None
    enh_transform = None
    if "asr_transform" in conf:
        asr_transform = support_transform("asr")(**conf["asr_transform"])
    if "enh_transform" in conf:
        enh_transform = support_transform("enh")(**conf["enh_transform"])

    # CTC
    if "ctc_coeff" in conf["trainer_conf"]:
        if conf["trainer_conf"]["ctc_coeff"] > 0:
            conf["nnet_conf"]["ctc"] = True
            if "<blank>" not in vocab:
                raise RuntimeError(
                    "Missing <blank> in dictionary for CTC training")
            conf["trainer_conf"]["ctc_blank"] = vocab["<blank>"]
    else:
        conf["nnet_conf"]["ctc"] = False

    # dump configurations
    with open(checkpoint / "train.yaml", "w") as f:
        yaml.dump(conf, f)

    if enh_transform:
        nnet = asr_cls(enh_transform=enh_transform,
                       asr_transform=asr_transform,
                       **conf["nnet_conf"])
    elif asr_transform:
        nnet = asr_cls(asr_transform=asr_transform, **conf["nnet_conf"])
    else:
        nnet = asr_cls(**conf["nnet_conf"])

    train_worker(args.local_rank, nnet, conf, args)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=
        "Command to start ASR model training, configured by yaml files "
        "(support distributed mode on single node). "
        "Using python -m torch.distributed.launch to launch the command.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--local_rank",
                        type=int,
                        default=0,
                        help="Local rank value, supplied automatically "
                        "by torch.distributed.launch")
    parser.add_argument("--num-process",
                        type=int,
                        default=1,
                        help="Number of process for distributed training")
    parser.add_argument("--conf",
                        type=str,
                        required=True,
                        help="Yaml configuration file for training")
    parser.add_argument("--dict",
                        type=str,
                        required=True,
                        help="Dictionary file")
    parser.add_argument("--epoches",
                        type=int,
                        default=50,
                        help="Number of training epoches")
    parser.add_argument("--checkpoint",
                        type=str,
                        required=True,
                        help="Directory to save models")
    parser.add_argument("--resume",
                        type=str,
                        default="",
                        help="Exist model to resume training from")
    parser.add_argument("--init",
                        type=str,
                        default="",
                        help="Exist model to initialize model training")
    parser.add_argument("--batch-size",
                        type=int,
                        default=32,
                        help="Number of utterances in each batch")
    parser.add_argument("--eval-interval",
                        type=int,
                        default=3000,
                        help="Number of batches trained per epoch "
                        "(for larger training dataset)")
    parser.add_argument("--save-interval",
                        type=int,
                        default=-1,
                        help="Interval to save the checkpoint")
    parser.add_argument("--prog-interval",
                        type=int,
                        default=100,
                        help="Interval to report the progress of the training")
    parser.add_argument("--num-workers",
                        type=int,
                        default=4,
                        help="Number of workers used in script data loader")
    parser.add_argument("--tensorboard",
                        action=StrToBoolAction,
                        default="false",
                        help="Flags to use the tensorboad")
    parser.add_argument("--seed",
                        type=int,
                        default=777,
                        help="Random seed used for random package")
    args = parser.parse_args()
    run(args)
