# Copyright 2019 Jian Wu
# License: Apache 2.0 (http://www.apache.org/licenses/LICENSE-2.0)

import math
import warnings

from pathlib import Path
from collections import defaultdict

import torch as th
from torch.nn.utils import clip_grad_norm_
from torch.optim.lr_scheduler import ReduceLROnPlateau
from typing import Optional, Dict, List, Union, Tuple, NoReturn, Iterable
from aps.trainer.ss import ss_scheduler_cls
from aps.trainer.lr import lr_scheduler_cls
from aps.utils import load_obj, get_device_ids, get_logger, SimpleTimer
from aps.task import Task

try:
    from torch.utils.tensorboard import SummaryWriter
    tensorboard_available = True
except ImportError:
    tensorboard_available = False


class WeightNoiseAdder(object):
    """
    Add gaussian noise to updated weights
    """

    def __init__(self, std: float = 0.075) -> None:
        # D(cX) = c^2 * D(X)
        self.factor = std**2

    def __call__(self, nnet: th.nn.Module) -> NoReturn:
        for p in nnet.parameters():
            if p.requires_grad:
                p.data += th.randn(p.data.shape,
                                   device=p.data.device) * self.factor


class ProgressReporter(object):
    """
    A simple training progress reporter
    """

    def __init__(self,
                 checkpoint: Path,
                 metrics: List[str],
                 period: int = 100,
                 tensorboard: bool = True,
                 rank: Optional[int] = None) -> None:
        self.period = period
        self.rank = rank
        # mkdir
        checkpoint.mkdir(parents=True, exist_ok=True)
        if rank is None:
            logger_loc = (checkpoint / "trainer.log").as_posix()
            self.header = "Trainer"
        else:
            logger_loc = (checkpoint / f"trainer.rank.{rank}.log").as_posix()
            self.header = f"Rank {rank}"

        self.logger = get_logger(logger_loc, file=True)
        # only for rank-0
        if tensorboard and rank in [0, None]:
            if not tensorboard_available:
                warnings.warn("tensorboard not installed thus disable it...")
                self.board_writer = None
            else:
                self.board_writer = SummaryWriter(checkpoint)
        else:
            self.board_writer = None
        self.metrics = metrics
        self.reset()

    def log(self, sstr: str) -> NoReturn:
        """
        Log messages
        """
        self.logger.info(f"{self.header}: {sstr}")

    def eval(self) -> NoReturn:
        """
        Reset to eval mode
        """
        self.log(">> Set eval mode ...")
        self.mode = "valid"
        self.reset()

    def train(self) -> NoReturn:
        """
        Reset to training mode
        """
        self.log(">> Set train mode ...")
        self.mode = "train"
        self.reset()

    def reset(self) -> NoReturn:
        """
        Clear the status
        """
        self.stats = defaultdict(list)
        self.timer = SimpleTimer()

    def update(self, dict_obj: Dict) -> NoReturn:
        """
        Track the recording items (multiple)
        """
        if dict_obj is None:
            return
        for key, value in dict_obj.items():
            if isinstance(value, th.Tensor):
                value = value.item()
            self.add(key, value)

    def add(self, key: str, value: float) -> NoReturn:
        """
        Track one recording item
        """
        self.stats[key].append(value)
        N = len(self.stats[key])
        if not N % self.period:
            if key == "rate":
                cur = self.stats[key][-1]
                self.log(f"Processed {N:.2e} batches ({key} = {cur:.3e}) ...")
            else:
                avg = sum(self.stats[key][-self.period:]) / self.period
                self.log(f"Processed {N:.2e} batches ({key} = {avg:+.2f}) ...")

    def report_metrics(self):
        """
        Report the tracked metrics (used for logging & scheduling)
        """
        reports = {}
        for metric in self.metrics:
            if metric not in self.stats:
                raise RuntimeError(
                    f"Metric {metric} is not tracked by the reporter")
            if metric == "accu":
                reports["accu"] = sum(self.stats["accu"]) * 100 / len(
                    self.stats["accu"])
            else:
                reports[metric] = sum(self.stats[metric]) / len(
                    self.stats[metric])
        return reports

    def report(self, epoch: int, lr: float) -> Tuple[Dict, str]:
        """
        Return the reports and log messages
        """
        N = len(self.stats["loss"])
        if self.mode == "valid":
            sstr = ",".join(
                map(lambda f: "{:.2f}".format(f), self.stats["loss"]))
            self.log(f"Loss on {N:d} batches: {sstr}")

        if N == 0:
            raise RuntimeError("No statistics to report")
        # Got reports
        reports = self.report_metrics()
        # Write tensorboard if needed
        if self.board_writer:
            for name, value in reports.items():
                self.board_writer.add_scalar(f"loss/{self.mode}", name, value)
        cost = self.timer.elapsed()

        header = "/".join(self.metrics)
        values = "/".join([f"{reports[metric]:.4f}" for metric in self.metrics])
        logstr = (f"Epoch {epoch:02d} ({self.mode}): {header}(time/#batch, " +
                  f"lr={lr:.3e}) = {values}({cost:.2f}m/{N:d})")
        return reports, logstr


class StopCriterion(object):
    """
    Manage the early stop of the training
    """

    def __init__(self,
                 no_impr: int,
                 mode: str = "min",
                 init_criterion: float = math.inf,
                 no_impr_thres: float = 2e-3) -> None:
        self.max_no_impr = no_impr
        self.no_impr = 0
        self.no_impr_thres = no_impr_thres
        self.mode = mode
        self.best_criterion = init_criterion

    def reset(self, update_value: float) -> NoReturn:
        """
        Reset the best criterion number
        """
        self.best_criterion = update_value

    def stop(self) -> bool:
        """
        Stop training or not
        """
        return self.no_impr == self.max_no_impr

    @property
    def best(self) -> float:
        """
        Return the tracked best criterion number
        """
        return self.best_criterion

    def step(self, update_value: float) -> bool:
        """
        Make one step
        """
        is_better = True
        # loss
        if self.mode == "min":
            is_better = self.best_criterion > update_value + self.no_impr_thres
        # accu
        if self.mode == "max":
            is_better = self.best_criterion < update_value - self.no_impr_thres
        if is_better:
            self.best_criterion = update_value
            self.no_impr = 0
            return True
        else:
            self.no_impr += 1
            return False


class Trainer(object):
    """
    A PyTorch distributed trainer
    """

    def __init__(self,
                 task: th.nn.Module,
                 rank: Optional[int] = None,
                 device_ids: Union[str, int, List[int]] = 0,
                 checkpoint: Union[str, Path] = "cpt",
                 optimizer: str = "adam",
                 optimizer_kwargs: Optional[Dict] = None,
                 lr_scheduler: str = "reduce_lr",
                 lr_scheduler_kwargs: Optional[Dict] = None,
                 lr_scheduler_period: str = "epoch",
                 ss_scheduler: str = "const",
                 ss_scheduler_kwargs: Optional[Dict] = None,
                 clip_gradient: Optional[float] = None,
                 weight_noise_std: Optional[float] = None,
                 prog_interval: int = 100,
                 save_interval: int = -1,
                 resume: str = "",
                 init: str = "",
                 tensorboard: bool = False,
                 stop_criterion: str = "loss",
                 no_impr: int = 6,
                 no_impr_thres: float = 1e-3,
                 report_metrics: List[str] = ["loss"],
                 **kwargs) -> None:
        if not isinstance(task, Task):
            raise TypeError(
                f"Trainer accepts Task object, but got {type(task)}")
        if lr_scheduler_period not in ["epoch", "step"]:
            raise ValueError(
                f"Unsupported lr_scheduler_period: {lr_scheduler_period}")
        if stop_criterion not in report_metrics:
            raise ValueError("stop_criterion is not included in " +
                             f"report_metrics: {stop_criterion}")
        if rank is not None and rank < 0:
            raise ValueError(f"Got invalid rank value: {rank}")
        if not isinstance(device_ids, tuple):
            device_ids = get_device_ids(device_ids)
        self.cuda_devices = len(device_ids)
        self.device_ids = device_ids

        if rank is None:
            # single GPU
            self.default_device = th.device(f"cuda:{device_ids[0]:d}")
        else:
            # in distributed mode
            if rank >= self.cuda_devices:
                raise ValueError("rank value exceeds number of GPUs: " +
                                 f"{rank} vs {self.cuda_devices}")
            self.default_device = th.device(f"cuda:{device_ids[rank]:d}")

        # avoid alloc memory from gpu0
        th.cuda.set_device(self.default_device)

        self.rank = rank
        self.checkpoint = Path(checkpoint)
        # if exist, resume training
        last_checkpoint = self.checkpoint / "last.pt.tar"
        if last_checkpoint.exists():
            resume = last_checkpoint.as_posix()

        self.reporter = ProgressReporter(self.checkpoint,
                                         report_metrics,
                                         rank=rank,
                                         period=prog_interval,
                                         tensorboard=tensorboard)
        if weight_noise_std is None:
            self.weight_noise_adder = None
        else:
            self.weight_noise_adder = WeightNoiseAdder(weight_noise_std)

        self.clip_gradient = clip_gradient
        self.cur_epoch = 0  # zero based
        self.cur_step = 0
        self.save_interval = save_interval
        self.ssr = 0
        self.no_impr = no_impr

        mode = "max" if stop_criterion == "accu" else "min"
        self.stop_on = stop_criterion
        self.stop_criterion = StopCriterion(no_impr,
                                            mode=mode,
                                            no_impr_thres=no_impr_thres)

        self.num_params = sum(
            [param.nelement() for param in task.nnet.parameters()]) / 10.0**6
        self.task = task
        if self.rank in [0, None]:
            self.reporter.log(f"Model summary:\n{task.nnet}")
        self.task.to(self.default_device)

        if resume or init:
            self.cpt_stats, optimizer_dict = self.load_checkpoint(
                resume if resume else init, "resume" if resume else "init")
            lr_scheduler_kwargs["state"] = self.cpt_stats["lr_scheduler_dict"]
        else:
            self.cpt_stats, optimizer_dict = None, None
            lr_scheduler_kwargs["state"] = None
        # make optimizer
        self.optimizer = self.create_optimizer(optimizer,
                                               optimizer_kwargs,
                                               state=optimizer_dict)

        # make lr scheduler
        if lr_scheduler == "reduce_lr":
            if lr_scheduler_period != "epoch":
                warnings.warn("For reduce_lr scheduler, lr_scheduler_period " +
                              "shoule be \'epoch\'")
                lr_scheduler_period = "epoch"
            reduce_lr_kwargs = {
                "mode": mode,
                "threshold_mode": "abs",
                "threshold": no_impr_thres
            }
            lr_scheduler_kwargs.update(reduce_lr_kwargs)
        self.lr_scheduler = self.create_scheduler(lr_scheduler, self.optimizer,
                                                  **lr_scheduler_kwargs)
        self.lr_scheduler_period = lr_scheduler_period

        # make ss scheduler
        if ss_scheduler_kwargs:
            if ss_scheduler not in ss_scheduler_cls:
                raise ValueError(f"Unsupported ss scheduler: {ss_scheduler}")
            if "accu" not in report_metrics:
                raise ValueError("When using schedule sampling, accu need to "
                                 "be tracked in report_metrics")
            self.ss_scheduler = ss_scheduler_cls[ss_scheduler](
                **ss_scheduler_kwargs)
            self.reporter.log(f"Using schedule sampling: {ss_scheduler}")
        else:
            self.ss_scheduler = None

        # logging
        if rank is None:
            self.reporter.log(f"Loading model to GPU:{device_ids[0]}, " +
                              f"#param: {self.num_params:.2f}M")
        else:
            self.reporter.log(
                f"Loading model to GPU-{rank}/{self.cuda_devices}, " +
                f"#param: {self.num_params:.2f}M")

        self.reporter.log(f"Track the metrics: {report_metrics}")
        self.reporter.log(f"Stop criterion: {self.stop_on}")
        if clip_gradient:
            self.reporter.log(
                f"Gradient clipping if over {clip_gradient} L2 norm")
        if weight_noise_std:
            self.reporter.log("Add gaussian noise to weights, with " +
                              f"std = {weight_noise_std}")

    def create_optimizer(self,
                         optimizer: str,
                         kwargs: Dict,
                         state: Optional[Dict] = None) -> th.optim.Optimizer:
        """
        Return a PyTorch optimizer
        """
        supported_optimizer = {
            "sgd": th.optim.SGD,  # momentum, weight_decay, lr
            "rmsprop": th.optim.RMSprop,  # momentum, weight_decay, lr
            "adam": th.optim.Adam,  # weight_decay, lr
            "adadelta": th.optim.Adadelta,  # weight_decay, lr
            "adagrad": th.optim.Adagrad,  # lr, lr_decay, weight_decay
            "adamax": th.optim.Adamax,  # lr, weight_decay
            "adamw": th.optim.AdamW,  # lr, weight_decay
            # ...
        }
        if optimizer not in supported_optimizer:
            raise ValueError(f"Unknown optimizer: {optimizer}")
        opt = supported_optimizer[optimizer](self.task.parameters(), **kwargs)
        self.reporter.log(f"Create optimizer {optimizer}: {kwargs}")
        if state is not None:
            opt.load_state_dict(state)
            self.reporter.log("Load optimizer state dict from checkpoint")
        return opt

    def create_scheduler(self,
                         scheduler: str,
                         optimizer: th.optim.Optimizer,
                         state: Optional[Dict] = None,
                         **kwargs):
        """
        Return a learning rate scheduler
        """
        if scheduler not in lr_scheduler_cls:
            raise ValueError(f"Unsupported lr scheduler: {scheduler}")
        lr_scheduler = lr_scheduler_cls[scheduler](optimizer, **kwargs)
        self.reporter.log(f"Create scheduler {scheduler}: {kwargs}")
        if state is not None:
            lr_scheduler.load_state_dict(state)
            self.reporter.log("Load scheduler state dict from checkpoint")
        return lr_scheduler

    def load_checkpoint(
            self,
            cpt_path: str,
            manner: str = "resume") -> Tuple[Optional[Dict], Optional[Dict]]:
        """
        Load checkpoint
        """
        if manner not in ["resume", "init"]:
            raise ValueError(f"Unsupported manner: {manner}")
        cpt_stats = th.load(cpt_path, map_location="cpu")
        cpt_epoch = cpt_stats["epoch"]
        cpt_loss = cpt_stats["loss"]
        cpt_step = cpt_stats["step"]
        self.task.nnet.load_state_dict(cpt_stats["model_state_dict"])
        optimizer_dict = None
        if manner == "resume":
            self.reporter.log(f"Resume from checkpoint {cpt_path}: " +
                              f"epoch/step {cpt_epoch}/{cpt_step}")
            optimizer_dict = cpt_stats["optim_state_dict"]
            # set current epoch/step number
            self.cur_epoch = cpt_epoch
            self.cur_step = cpt_step
        else:
            self.reporter.log(f"Intialize from checkpoint {cpt_path}: " +
                              f"epoch/step {cpt_epoch}/{cpt_step}")
        self.reporter.log(f"Loss tracked in the checkpoint: {cpt_loss:.3f}")
        return cpt_stats, optimizer_dict

    def model_states(self) -> Dict:
        """
        Return model states which will be saved in the checkpoint
        """
        raise NotImplementedError

    def save_checkpoint(self, states: Dict, best: bool = True) -> NoReturn:
        """
        Save checkpoint (epoch, model, optimizer, ...)
        """
        if self.rank in [0, None]:
            cpt = self.model_states()
            cpt.update(states)
            cpt_name = "{}.pt.tar".format("best" if best else "last")
            th.save(cpt, self.checkpoint / cpt_name)
            self.reporter.log(f"Save checkpoint {self.checkpoint / cpt_name}")
            if self.save_interval > 0 and self.cur_epoch % self.save_interval == 0:
                th.save(cpt, self.checkpoint / f"{self.cur_epoch}.pt.tar")

    def train_one_step(self, egs: Dict) -> bool:
        """
        Make one training step (return true if no error exists)

        1) Zero optimizer
        2) Forward & Backword
        3) Clip Gradient
        4) Step optimizer
        """
        self.optimizer.zero_grad()

        stats = self.task(egs)
        loss = stats["loss"].item()
        # backward if not nan/inf
        if math.isfinite(loss):
            stats["loss"].backward()
        else:
            self.reporter.log(f"Invalid loss {loss:.3f}, skip...")
            return False

        # clip gradient after backward
        norm = -1
        if self.clip_gradient:
            norm = clip_grad_norm_(self.task.parameters(), self.clip_gradient)

        # step optimizer and update statistics
        if math.isfinite(norm):
            self.optimizer.step()
            if norm != -1:
                stats["norm"] = norm
            stats["rate"] = self.optimizer.param_groups[0]["lr"]
            self.reporter.update(stats)
            # add noise if needed
            if self.weight_noise_adder:
                self.weight_noise_adder(self.task)
            # schedule lr if needed
            self.lr_scheduler_step(None, end_at="step")
            return True
        else:
            self.reporter.log(f"Invalid gradient {norm:.3f}, skip...")
            return False

    def lr_scheduler_step(self,
                          update_value: Optional[float],
                          end_at: str = "epoch") -> NoReturn:
        """
        Make one step in lr scheduler
        """
        if end_at == "step" and self.lr_scheduler_period == "step":
            self.lr_scheduler.step()
        if end_at == "epoch" and self.lr_scheduler_period == "epoch":
            if isinstance(self.lr_scheduler, ReduceLROnPlateau):
                self.lr_scheduler.step(update_value)
            else:
                self.lr_scheduler.step()

    def train_epoch(self, data_loader: Iterable[Dict]) -> NoReturn:
        """
        Run one training epoch
        """
        self.task.train()
        self.reporter.train()
        # for idx, egs in enumerate(data_loader):
        for egs in data_loader:
            # load to gpu
            egs = self.prep_egs(egs)
            # make one training step
            if self.train_one_step(egs):
                self.cur_step += 1

    def valid_epoch(self, data_loader: Iterable[Dict]) -> NoReturn:
        """
        Run one validation epoch
        """
        self.task.eval()
        self.reporter.eval()

        with th.no_grad():
            for egs in data_loader:
                # load to gpu
                egs = self.prep_egs(egs)
                stats = self.task(egs)
                # update statistics
                self.reporter.update(stats)

    def stop_detect(self, dev_loader: Iterable[Dict], lr: float) -> bool:
        """
        Run valid epoch and schedule training progress:

        1) schedule learning/sampling rate
        2) save checkpoint
        3) early stop detection
        """
        self.valid_epoch(dev_loader)
        reports, logstr = self.reporter.report(self.cur_epoch, lr)
        # schedule sampling for eval
        if self.ss_scheduler:
            logstr += f" | ssr = {self.ssr:.3f}"

        update_value = reports[self.stop_on]
        better = self.stop_criterion.step(update_value)

        status = {
            "step": self.cur_step,
            "epoch": self.cur_epoch,
            "optim_state_dict": self.optimizer.state_dict(),
            "lr_scheduler_dict": self.lr_scheduler.state_dict()
        }
        status.update(reports)
        if better:
            self.save_checkpoint(status, best=True)
        else:
            logstr += f" | no impr: {self.stop_criterion.no_impr:d}, "
            logstr += f"best = {self.stop_criterion.best:.4f}"

        self.reporter.log(logstr)
        # << valid
        # lr schedule here
        self.lr_scheduler_step(update_value, end_at="epoch")
        if self.ss_scheduler:
            self.ssr = self.ss_scheduler.step(self.cur_epoch, reports["accu"])
        # save last checkpoint
        self.save_checkpoint(status, best=False)
        # early stop
        if self.stop_criterion.stop():
            self.reporter.log("Stop training cause no impr for " +
                              f"{self.no_impr} epochs")
            return True
        return False

    def prep_egs(self, egs: Dict) -> Dict:
        """
        Prepare training egs
        """
        egs = load_obj(egs, self.default_device)
        # use ssr = 0 when in eval mode
        if self.ss_scheduler:
            egs["ssr"] = self.ssr if self.task.training else 0
        return egs

    def prep_run(self, dev_loader: Iterable[Dict]) -> int:
        """
        Prepare for training
        """
        # valid
        self.valid_epoch(dev_loader)
        cur_lr = self.optimizer.param_groups[0]["lr"]
        reports, logstr = self.reporter.report(self.cur_epoch, cur_lr)
        self.reporter.log(logstr)
        if self.ss_scheduler:
            self.ssr = self.ss_scheduler.step(self.cur_epoch, reports["accu"])
        # make sure not inf
        best_value = reports[self.stop_on]
        # for ReduceLROnPlateau
        if hasattr(self.lr_scheduler, "best"):
            self.lr_scheduler.best = best_value
        self.stop_criterion.reset(best_value)

    def run_in_epoch(self,
                     trn_loader: Iterable[Dict],
                     dev_loader: Iterable[Dict],
                     num_epochs: int = 50) -> int:
        """
        Running in epoch mode: treat whole training set as one training epoch
        """
        while self.cur_epoch < num_epochs:
            trn_loader.set_epoch(self.cur_epoch)
            self.cur_epoch += 1
            # >> train
            self.train_epoch(trn_loader)
            cur_lr = self.optimizer.param_groups[0]["lr"]
            _, logstr = self.reporter.report(self.cur_epoch, cur_lr)
            self.reporter.log(logstr)
            # << train
            if self.stop_detect(dev_loader, cur_lr):
                break
        return self.cur_epoch

    def run_in_batch(self,
                     trn_loader: Iterable[Dict],
                     dev_loader: Iterable[Dict],
                     num_epochs: int = 100,
                     eval_interval: int = 3000) -> int:
        """
        Running in batch mode: for large training set, treat several batches as one training epoch
        """
        stop = False
        while True:
            # trained on several batches
            for egs in trn_loader:
                # enable train mode
                if self.cur_step % eval_interval == 0:
                    self.task.train()
                    self.reporter.train()
                    trn_loader.set_epoch(self.cur_epoch)
                # update per-batch
                egs = self.prep_egs(egs)
                succ = self.train_one_step(egs)
                if succ:
                    self.cur_step = (self.cur_step + 1) % eval_interval
                # if trained on batches done, start evaluation
                if self.cur_step % eval_interval == 0 and succ:
                    self.cur_epoch += 1
                    cur_lr = self.optimizer.param_groups[0]["lr"]
                    _, logstr = self.reporter.report(self.cur_epoch, cur_lr)
                    self.reporter.log(logstr)
                    end = self.stop_detect(dev_loader, cur_lr)
                    if end or self.cur_epoch == num_epochs:
                        stop = True
                        break
            if stop:
                break
            self.reporter.log("Finished one epoch on training set")
        return self.cur_epoch

    def run(self,
            trn_loader: Iterable[Dict],
            dev_loader: Iterable[Dict],
            num_epochs: int = 100,
            eval_interval: int = -1) -> NoReturn:
        """
        Entry of the Trainer class
        """
        self.reporter.log(
            f"Number of batches: {len(trn_loader)}/{len(dev_loader)}")
        self.prep_run(dev_loader)
        if eval_interval > 0:
            done_epoch = self.run_in_batch(trn_loader,
                                           dev_loader,
                                           num_epochs=num_epochs,
                                           eval_interval=eval_interval)
        else:
            done_epoch = self.run_in_epoch(trn_loader,
                                           dev_loader,
                                           num_epochs=num_epochs)
        self.reporter.log(
            f"Training for {done_epoch:d}/{num_epochs:d} epochs done!")
