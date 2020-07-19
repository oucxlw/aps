#!/usr/bin/env python

# wujian@2019

import torch as th
import torch.nn as nn

import torch.nn.functional as tf

from torch.nn.utils.rnn import pad_packed_sequence, pack_padded_sequence


def encoder_instance(encoder_type, input_size, output_size, **kwargs):
    """
    Return encoder instance
    """
    supported_encoder = {
        "common": TorchRNNEncoder,
        "custom": CustomRNNEncoder,
        "tdnn": TimeDelayRNNEncoder,
        "fsmn": TimeDelayFSMNEncoder
    }
    if encoder_type not in supported_encoder:
        raise RuntimeError(f"Unknown encoder type: {encoder_type}")
    return supported_encoder[encoder_type](input_size, output_size, **kwargs)


support_encoder_act = {
    "relu": tf.relu,
    "sigmoid": th.sigmoid,
    "tanh": th.tanh,
    "": None
}


class TorchRNNEncoder(nn.Module):
    """
    PyTorch's RNN encoder
    """
    def __init__(self,
                 input_size,
                 output_size,
                 input_project=None,
                 rnn="lstm",
                 rnn_layers=3,
                 rnn_hidden=512,
                 rnn_dropout=0.2,
                 rnn_bidir=False,
                 non_linear=""):
        super(TorchRNNEncoder, self).__init__()
        RNN = rnn.upper()
        supported_rnn = {"LSTM": nn.LSTM, "GRU": nn.GRU, "RNN": nn.RNN}
        support_non_linear = {
            "relu": tf.relu,
            "sigmoid": th.sigmoid,
            "tanh": th.tanh,
            "": None
        }
        if RNN not in supported_rnn:
            raise RuntimeError(f"Unknown RNN type: {RNN}")
        if non_linear not in support_non_linear:
            raise ValueError(
                f"Unsupported output non-linear function: {non_linear}")
        if input_project:
            self.proj = nn.Linear(input_size, input_project)
        else:
            self.proj = None
        self.rnns = supported_rnn[RNN](
            input_size if input_project is None else input_project,
            rnn_hidden,
            rnn_layers,
            batch_first=True,
            dropout=rnn_dropout,
            bidirectional=rnn_bidir)
        self.outp = nn.Linear(rnn_hidden if not rnn_bidir else rnn_hidden * 2,
                              output_size)
        self.non_linear = support_non_linear[non_linear]

    def flat(self):
        self.rnn.flatten_parameters()

    def forward(self, inp_pad, inp_len, max_len=None):
        """
        Args:
            inp_pad (Tensor): (N) x Ti x F
            inp_len (Tensor): (N) x Ti
        Return:
            y_pad (Tensor): (N) x Ti x F
            y_len (Tensor): (N) x Ti
        """
        self.rnns.flatten_parameters()
        if inp_len is not None:
            inp_pad = pack_padded_sequence(inp_pad, inp_len, batch_first=True)
        # extend dim when inference
        else:
            if inp_pad.dim() not in [2, 3]:
                raise RuntimeError("TorchRNNEncoder expects 2/3D Tensor, " +
                                   f"got {inp_pad.dim():d}")
            if inp_pad.dim() != 3:
                inp_pad = th.unsqueeze(inp_pad, 0)
        if self.proj:
            inp_pad = tf.relu(self.proj(inp_pad))
        y, _ = self.rnns(inp_pad)
        # using unpacked sequence
        # y: NxTxD
        if inp_len is not None:
            y, _ = pad_packed_sequence(y,
                                       batch_first=True,
                                       total_length=max_len)
        y = self.outp(y)
        # pass through non-linear
        if self.non_linear:
            y = self.non_linear(y)
        return y, inp_len


class CustomRNNLayer(nn.Module):
    """
    A custom rnn layer for PyramidEncoder
    """
    def __init__(self,
                 input_size,
                 hidden_size=512,
                 project_size=None,
                 rnn="lstm",
                 layernorm=False,
                 dropout=0.0,
                 bidirectional=False,
                 add_forward_backward=False):
        super(CustomRNNLayer, self).__init__()
        RNN = rnn.upper()
        supported_rnn = {"LSTM": nn.LSTM, "GRU": nn.GRU, "RNN": nn.RNN}
        if RNN not in supported_rnn:
            raise RuntimeError(f"Unknown RNN type: {RNN}")
        self.rnn = supported_rnn[RNN](input_size,
                                      hidden_size,
                                      1,
                                      batch_first=True,
                                      bidirectional=bidirectional)
        self.add = add_forward_backward and bidirectional
        self.dropout = nn.Dropout(dropout) if dropout != 0 else None
        if not add_forward_backward and bidirectional:
            hidden_size *= 2
        self.layernorm = nn.LayerNorm(hidden_size) if layernorm else None
        self.proj = nn.Linear(hidden_size,
                              project_size) if project_size else None

    def flat(self):
        self.rnn.flatten_parameters()

    def forward(self, x_pad, x_len):
        """
        args:
            x_pad: (N) x Ti x F
            x_len: (N) x Ti
        """
        max_len = x_pad.size(1)
        if x_len is not None:
            x_pad = pack_padded_sequence(x_pad, x_len, batch_first=True)
        # extend dim when inference
        else:
            if x_pad.dim() not in [2, 3]:
                raise RuntimeError("RNN expect input dim as 2 or 3, " +
                                   f"got {x_pad.dim()}")
            if x_pad.dim() != 3:
                x_pad = th.unsqueeze(x_pad, 0)
        y, _ = self.rnn(x_pad)
        # x: NxTxD
        if x_len is not None:
            y, _ = pad_packed_sequence(y,
                                       batch_first=True,
                                       total_length=max_len)
        # add forward & backward
        if self.add:
            f, b = th.chunk(y, 2, dim=-1)
            y = f + b
        # dropout
        if self.dropout:
            y = self.dropout(y)
        # add ln
        if self.layernorm:
            y = self.layernorm(y)
        # proj
        if self.proj:
            y = self.proj(y)
        return y


class CustomRNNEncoder(nn.Module):
    """
    Customized RNN layer (egs: PyramidEncoder)
    """
    def __init__(self,
                 input_size,
                 output_size,
                 rnn="lstm",
                 rnn_layers=3,
                 rnn_bidir=True,
                 rnn_dropout=0.0,
                 rnn_hidden=512,
                 rnn_project=None,
                 layernorm=False,
                 use_pyramid=False,
                 add_forward_backward=False):
        super(CustomRNNEncoder, self).__init__()

        def derive_in_size(layer_idx):
            """
            Compute input size of layer-i
            """
            if layer_idx == 0:
                in_size = input_size
            else:
                if rnn_project:
                    return rnn_project
                else:
                    in_size = rnn_hidden
                    if rnn_bidir and not add_forward_backward:
                        in_size = in_size * 2
                if use_pyramid:
                    in_size = in_size * 2
            return in_size

        rnn_list = []
        for i in range(rnn_layers):
            rnn_list.append(
                CustomRNNLayer(derive_in_size(i),
                               hidden_size=rnn_hidden,
                               rnn=rnn,
                               project_size=rnn_project
                               if i != rnn_layers - 1 else output_size,
                               layernorm=layernorm,
                               dropout=rnn_dropout,
                               bidirectional=rnn_bidir,
                               add_forward_backward=add_forward_backward))
        self.rnns = nn.ModuleList(rnn_list)
        self.use_pyramid = use_pyramid

    def flat(self):
        for layer in self.rnns:
            layer.flat()

    def _ds(self, x_pad, x_len):
        """
        Do downsampling for RNN output
        """
        _, T, _ = x_pad.shape
        # concat
        if T % 2:
            x_pad = x_pad[:, :-1]
        ctx = [x_pad[:, ::2], x_pad[:, 1::2]]
        x_pad = th.cat(ctx, -1)
        if x_len is not None:
            x_len = x_len // 2
        return x_pad, x_len

    def forward(self, x_pad, x_len):
        """
        args:
            x_pad: (N) x Ti x F
            x_len: (N) x Ti
        """
        for index, layer in enumerate(self.rnns):
            if index != 0 and self.use_pyramid:
                x_pad, x_len = self._ds(x_pad, x_len)
            x_pad = layer(x_pad, x_len)
        return x_pad, x_len


class TimeDelayLayer(nn.Module):
    """
    Implement a TDNN layer using conv1d operations
    """
    def __init__(self,
                 input_size,
                 output_size,
                 kernel_size=3,
                 steps=2,
                 dilation=1,
                 norm="BN",
                 dropout=0):
        super(TimeDelayLayer, self).__init__()
        if norm not in ["BN", "LN"]:
            raise ValueError(f"Unsupported normalization layers: {norm}")
        self.conv1d = nn.Conv1d(input_size,
                                output_size,
                                kernel_size,
                                stride=steps,
                                dilation=dilation,
                                padding=(dilation * (kernel_size - 1)) // 2)
        if norm == "BN":
            self.norm = nn.BatchNorm1d(output_size)
        else:
            self.norm = nn.LayerNorm(output_size)
        self.dropout = nn.Dropout(p=dropout) if dropout > 0 else None
        self.ds = steps

    def forward(self, x):
        """
        args:
            x: (N) x T x F
        """
        if x.dim() not in [2, 3]:
            raise RuntimeError(
                f"TimeDelayLayer accepts 2/3D tensor, got {x.dim()} instead")
        if x.dim() == 2:
            x = x[None, ...]
        # N x T x F => N x F x T
        x = x.transpose(1, 2)
        # conv & bn
        y = self.conv1d(x)
        y = y[..., :x.shape[-1] // self.ds]
        y = tf.relu(y)
        # N x F x T
        if isinstance(self.norm, nn.BatchNorm1d):
            # N x F x T => N x T x F
            y = self.norm(y)
            y = y.transpose(1, 2)
        else:
            y = y.transpose(1, 2)
            y = self.norm(y)
        if self.dropout:
            y = self.dropout(y)
        return y


class FSMNLayer(nn.Module):
    """
    Implement layer of feedforward sequential memory networks (FSMN)
    """
    def __init__(self,
                 input_size,
                 output_size,
                 project_size,
                 lctx=3,
                 rctx=3,
                 norm="BN",
                 dilation=0,
                 dropout=0):
        super(FSMNLayer, self).__init__()
        self.inp_proj = nn.Linear(input_size, project_size, bias=False)
        self.ctx_size = lctx + rctx + 1
        self.ctx_conv = nn.Conv1d(project_size,
                                  project_size,
                                  kernel_size=self.ctx_size,
                                  dilation=dilation,
                                  groups=project_size,
                                  padding=(self.ctx_size - 1) // 2,
                                  bias=False)
        self.out_proj = nn.Linear(project_size, output_size)
        if norm == "BN":
            self.norm = nn.BatchNorm1d(output_size)
        elif norm == "LN":
            self.norm = nn.LayerNorm(output_size)
        else:
            self.norm = None
        self.out_drop = nn.Dropout(p=dropout) if dropout > 0 else None

    def forward(self, x, m=None):
        """
        args:
            x: N x T x F, current input
            m: N x T x F, memory blocks from previous layer
        """
        if x.dim() not in [2, 3]:
            raise RuntimeError(f"FSMNLayer expect 2/3D input, got {x.dim()}")
        if x.dim() == 2:
            x = x[None, ...]
        # N x T x P
        p = self.inp_proj(x)
        # N x T x P => N x P x T => N x T x P
        p = p.transpose(1, 2)
        # add context
        p = p + self.ctx_conv(p)
        p = p.transpose(1, 2)
        # add memory block
        if m is not None:
            p = p + m
        # N x T x O
        o = tf.relu(self.out_proj(p))
        if self.norm:
            if isinstance(self.norm, nn.LayerNorm):
                o = self.norm(o)
            else:
                o = o.transpose(1, 2)
                o = self.norm(o)
                o = o.transpose(1, 2)
        if self.out_drop:
            o = self.out_drop(o)
        # N x T x O
        return o, p


def parse_str_int(str_or_int, num_layers):
    """
    Parse string or int, egs:
        1,1,2 => [1, 1, 2]
        2     => [2, 2, 2]
    """
    if isinstance(str_or_int, str):
        values = [int(t) for t in str_or_int.split(",")]
        if len(values) != num_layers:
            raise ValueError(f"Number of the layers: {num_layers} " +
                             f"do not match {str_or_int}")
    else:
        values = [str_or_int] * num_layers
    return values


class FSMNEncoder(nn.Module):
    """
    Stack of FsmnLayers, with optional residual connection
    """
    def __init__(self,
                 input_size,
                 output_size,
                 project_size,
                 num_layers=4,
                 residual=True,
                 lctx=3,
                 rctx=3,
                 norm="BN",
                 dilation=1,
                 dropout=0):
        super(FSMNEncoder, self).__init__()
        dilations = parse_str_int(dilation, num_layers)
        self.layers = nn.ModuleList([
            FSMNLayer(input_size,
                      output_size,
                      project_size,
                      lctx=lctx,
                      rctx=rctx,
                      norm="" if i == num_layers - 1 else norm,
                      dilation=dilations[i],
                      dropout=dropout) for i in range(num_layers)
        ])
        self.res = residual

    def forward(self, x):
        """
        args:
            x: N x T x F, input
        """
        if x.dim() not in [2, 3]:
            raise RuntimeError(f"FSMNEncoder expect 2/3D input, got {x.dim()}")
        if x.dim() == 2:
            x = x[None, ...]
        m = None
        for fsmn in self.layers:
            if self.res:
                x, m = fsmn(x, m=m)
            else:
                x, _ = fsmn(x, m=m)
        return x


class TimeDelayRNNEncoder(nn.Module):
    """
    TDNN + RNN encoder (Using TDNN for subsampling and RNN for sequence modeling )
    """
    def __init__(self,
                 input_size,
                 output_size,
                 tdnn_dim=512,
                 tdnn_norm="BN",
                 tdnn_layers=2,
                 tdnn_stride="2,2",
                 tdnn_dilation="1,1",
                 tdnn_dropout=0,
                 rnn="lstm",
                 rnn_layers=3,
                 rnn_bidir=True,
                 rnn_dropout=0.2,
                 rnn_project=None,
                 rnn_layernorm=False,
                 rnn_hidden=512):
        super(TimeDelayRNNEncoder, self).__init__()
        stride_conf = parse_str_int(tdnn_stride, tdnn_layers)
        dilation_conf = parse_str_int(tdnn_dilation, tdnn_layers)
        tdnns = []
        self.tdnn_layers = tdnn_layers
        for i in range(tdnn_layers):
            tdnns.append(
                TimeDelayLayer(input_size if i == 0 else tdnn_dim,
                               tdnn_dim,
                               kernel_size=3,
                               norm=tdnn_norm,
                               steps=stride_conf[i],
                               dilation=dilation_conf[i],
                               dropout=tdnn_dropout))
        self.tdnn_enc = nn.Sequential(*tdnns)
        self.rnns_enc = CustomRNNEncoder(tdnn_dim,
                                         output_size,
                                         rnn=rnn,
                                         layernorm=rnn_layernorm,
                                         rnn_layers=rnn_layers,
                                         rnn_bidir=rnn_bidir,
                                         rnn_dropout=rnn_dropout,
                                         rnn_project=rnn_project,
                                         rnn_hidden=rnn_hidden)

    def forward(self, x_pad, x_len):
        """
        args:
            x_pad: (N) x Ti x F
            x_len: (N) x Ti
        """
        if x_len is not None:
            x_len = x_len // (2**self.tdnn_layers)
        x_pad = self.tdnn_enc(x_pad)
        return self.rnns_enc(x_pad, x_len)


class TimeDelayFSMNEncoder(nn.Module):
    """
    TDNN + FSMN encoder (Using TDNN for subsampling and FSMN for sequence modeling )
    """
    def __init__(self,
                 input_size,
                 output_size,
                 tdnn_dim=512,
                 tdnn_norm="BN",
                 tdnn_layers=2,
                 tdnn_stride="2,2",
                 tdnn_dilation="1,1",
                 tdnn_dropout=0.2,
                 fsmn_layers=4,
                 fsmn_lctx=10,
                 fsmn_rctx=10,
                 fsmn_norm="LN",
                 fsmn_residual=True,
                 fsmn_dilation=1,
                 fsmn_project=512,
                 fsmn_dropout=0.2):
        super(TimeDelayFSMNEncoder, self).__init__()
        stride_conf = parse_str_int(tdnn_stride, tdnn_layers)
        dilation_conf = parse_str_int(tdnn_dilation, tdnn_layers)
        tdnns = []
        self.tdnn_layers = tdnn_layers
        for i in range(tdnn_layers):
            tdnns.append(
                TimeDelayLayer(input_size if i == 0 else tdnn_dim,
                               tdnn_dim,
                               kernel_size=3,
                               norm=tdnn_norm,
                               steps=stride_conf[i],
                               dilation=dilation_conf[i],
                               dropout=tdnn_dropout))
        self.tdnn_enc = nn.Sequential(*tdnns)
        self.fsmn_enc = FSMNEncoder(tdnn_dim,
                                    output_size,
                                    fsmn_project,
                                    lctx=fsmn_lctx,
                                    rctx=fsmn_rctx,
                                    norm=fsmn_norm,
                                    dilation=fsmn_dilation,
                                    residual=fsmn_residual,
                                    num_layers=fsmn_layers,
                                    dropout=fsmn_dropout)

    def forward(self, x_pad, x_len):
        """
        args:
            x_pad: (N) x Ti x F
            x_len: (N) x Ti
        """
        if x_len is not None:
            x_len = x_len // (2**self.tdnn_layers)
        x_pad = self.tdnn_enc(x_pad)
        x_pad = self.fsmn_enc(x_pad)
        return x_pad, x_len