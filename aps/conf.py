# Copyright 2020 Jian Wu
# License: Apache 2.0 (http://www.apache.org/licenses/LICENSE-2.0)
"""
Load {am,lm,se} training configurations
"""
import yaml
import codecs

from typing import Dict, List, Tuple

required_keys = [
    "nnet", "nnet_conf", "task", "task_conf", "data_conf", "trainer_conf"
]
all_ss_conf_keys = required_keys + ["enh_transform", "cmd_args"]
all_am_conf_keys = required_keys + [
    "enh_transform", "asr_transform", "cmd_args"
]
all_lm_conf_keys = required_keys + ["cmd_args"]


def load_dict(dict_path: str,
              reverse: bool = False,
              required: List[str] = ["<sos>", "<eos>"]) -> Dict:
    """
    Load the dictionary object
    Args:
        dict_path: path of the vocabulary dictionary
        required: required units in the dict
        reverse: return int:str if true, else str:int
    """
    with codecs.open(dict_path, mode="r", encoding="utf-8") as fd:
        vocab = {}
        for line in fd:
            tok, idx = line.split()
            if reverse:
                vocab[int(idx)] = tok
            else:
                if tok in vocab:
                    raise RuntimeError(
                        f"Duplicated token in {dict_path}: {tok}")
                vocab[tok] = int(idx)
    if not reverse:
        for token in required:
            if token not in vocab:
                raise ValueError(f"Miss token: {token} in {dict_path}")
    return vocab


def dump_dict(dict_path: str, vocab_dict: Dict, reverse: bool = False) -> None:
    """
    Dump the dictionary out
    """
    dict_str = []
    # <int, str> if reverse else <str, int>
    for key, val in vocab_dict.items():
        if reverse:
            val, key = key, val
        dict_str.append(f"{key} {val:d}")
    with codecs.open(dict_path, mode="w", encoding="utf-8") as fd:
        fd.write("\n".join(dict_str))


def check_conf(conf: Dict, required_keys: List[str],
               all_keys: List[str]) -> Dict:
    """
    Check the format of the configurations
    """
    # check the invalid item
    for key in conf.keys():
        if key not in all_keys:
            raise ValueError(f"Get invalid configuration item: {key}")
    # create task_conf if None
    if "task_conf" not in conf:
        conf["task_conf"] = {}
    # check the missing items
    for key in required_keys:
        if key not in conf.keys():
            raise ValueError(f"Miss the item in the configuration: {key}?")
    return conf


def load_ss_conf(yaml_conf: str) -> Dict:
    """
    Load yaml configurations for speech separation/enhancement tasks
    """
    with open(yaml_conf, "r") as f:
        conf = yaml.full_load(f)
    return check_conf(conf, required_keys, all_ss_conf_keys)


def load_lm_conf(yaml_conf: str, dict_path: str) -> Dict:
    """
    Load yaml configurations for language model training
    """
    with open(yaml_conf, "r") as f:
        conf = yaml.full_load(f)
    conf = check_conf(conf, required_keys, all_lm_conf_keys)
    vocab = load_dict(dict_path)
    conf["nnet_conf"]["vocab_size"] = len(vocab)
    return conf, vocab


def load_am_conf(yaml_conf: str, dict_path: str) -> Tuple[Dict, Dict]:
    """
    Load yaml configurations for acoustic model training
    """
    with open(yaml_conf, "r") as f:
        conf = yaml.full_load(f)
    conf = check_conf(conf, required_keys, all_am_conf_keys)

    # add dict info
    nnet_conf = conf["nnet_conf"]
    vocab = load_dict(
        dict_path,
        required=[] if conf["task"] == "asr@ctc" else ["<eos>", "<sos>"])
    nnet_conf["vocab_size"] = len(vocab)

    task_conf = conf["task_conf"]
    use_ctc = "ctc_weight" in task_conf and task_conf["ctc_weight"] > 0
    is_transducer_or_ctc = conf["task"] in ["asr@transducer", "asr@ctc"]
    if not is_transducer_or_ctc:
        nnet_conf["sos"] = vocab["<sos>"]
        nnet_conf["eos"] = vocab["<eos>"]
    # for CTC/RNNT
    if use_ctc or is_transducer_or_ctc:
        conf["task_conf"]["blank"] = len(vocab)
        # add blank
        nnet_conf["vocab_size"] += 1
        if use_ctc:
            nnet_conf["ctc"] = use_ctc
    return conf, vocab
