#!/usr/bin/env python

# Copyright 2020 Jian Wu
# License: Apache 2.0 (http://www.apache.org/licenses/LICENSE-2.0)

import math
import pytest
import librosa
import torch as th

from aps.transform.utils import forward_stft, inverse_stft
from aps.cplx import ComplexTensor
from aps.loader import read_audio
from aps.transform import AsrTransform, EnhTransform, FixedBeamformer, DfTransform
from aps.transform.asr import SpeedPerturbTransform

egs1_wav = read_audio("data/transform/egs1.wav", sr=16000)
egs2_wav = read_audio("data/transform/egs2.wav", sr=16000)


@pytest.mark.parametrize("wav", [egs1_wav])
@pytest.mark.parametrize("mode", ["librosa", "torch"])
@pytest.mark.parametrize("frame_len, frame_hop", [(512, 256), (1024, 256),
                                                  (256, 128)])
@pytest.mark.parametrize("window", ["hamm", "sqrthann"])
def test_forward_inverse_stft(wav, frame_len, frame_hop, window, mode):
    wav = th.from_numpy(wav)[None, ...]
    mid = forward_stft(wav,
                       frame_len,
                       frame_hop,
                       mode=mode,
                       window=window,
                       center=True,
                       return_polar=False)
    out = inverse_stft(mid,
                       frame_len,
                       frame_hop,
                       window=window,
                       center=True,
                       mode=mode,
                       return_polar=False)
    trunc = min(out.shape[-1], wav.shape[-1])
    th.testing.assert_allclose(out[..., :trunc], wav[..., :trunc])


@pytest.mark.parametrize("wav", [egs1_wav, egs2_wav[0].copy()])
@pytest.mark.parametrize("frame_len, frame_hop", [(512, 256), (1024, 256),
                                                  (400, 160)])
@pytest.mark.parametrize("window", ["hann", "hamm"])
@pytest.mark.parametrize("center", [False, True])
def test_with_librosa_stft(wav, frame_len, frame_hop, window, center):
    pack1 = forward_stft(th.from_numpy(wav)[None, ...],
                         frame_len,
                         frame_hop,
                         mode="librosa",
                         window=window,
                         center=center,
                         return_polar=False)
    pack2 = forward_stft(th.from_numpy(wav)[None, ...],
                         frame_len,
                         frame_hop,
                         mode="torch",
                         window=window,
                         center=center,
                         return_polar=False)
    librosa_stft = librosa.stft(wav,
                                n_fft=2**math.ceil(math.log2(frame_len)),
                                hop_length=frame_hop,
                                win_length=frame_len,
                                window=window,
                                center=center)
    librosa_real = th.tensor(librosa_stft.real, dtype=th.float32)
    librosa_imag = th.tensor(librosa_stft.imag, dtype=th.float32)
    th.testing.assert_allclose(pack1[..., 0], librosa_real)
    th.testing.assert_allclose(pack1[..., 1], librosa_imag)
    th.testing.assert_allclose(pack2[..., 0], librosa_real)
    th.testing.assert_allclose(pack2[..., 1], librosa_imag)


@pytest.mark.parametrize("wav", [egs1_wav])
@pytest.mark.parametrize("mode", ["librosa", "torch"])
@pytest.mark.parametrize("feats,shape", [("spectrogram-log", [1, 807, 257]),
                                         ("emph-fbank-log-cmvn", [1, 807, 80]),
                                         ("mfcc", [1, 807, 13]),
                                         ("mfcc-aug", [1, 807, 13]),
                                         ("mfcc-splice", [1, 807, 39]),
                                         ("mfcc-aug-delta", [1, 807, 39])])
def test_asr_transform(wav, mode, feats, shape):
    transform = AsrTransform(feats=feats,
                             stft_mode=mode,
                             frame_len=400,
                             frame_hop=160,
                             use_power=True,
                             pre_emphasis=0.96,
                             aug_prob=0.5,
                             aug_mask_zero=False)
    feats, _ = transform(th.from_numpy(wav[None, ...]), None)
    assert feats.shape == th.Size(shape)
    assert th.sum(th.isnan(feats)) == 0
    assert transform.feats_dim == shape[-1]


@pytest.mark.parametrize("max_length", [160000])
def test_speed_perturb(max_length):
    for _ in range(4):
        speed_perturb = SpeedPerturbTransform(sr=16000)
        wav_len = th.randint(max_length // 2, max_length, (1,))
        wav_out = speed_perturb(th.randn(1, wav_len.item()))
        out_len = speed_perturb.output_length(wav_len)
        assert wav_out.shape[-1] == out_len.item()


@pytest.mark.parametrize("wav", [egs2_wav])
@pytest.mark.parametrize("feats,shape",
                         [("spectrogram-log-cmvn-aug-ipd", [1, 366, 257 * 5]),
                          ("ipd", [1, 366, 257 * 4])])
def test_enh_transform(wav, feats, shape):
    transform = EnhTransform(feats=feats,
                             frame_len=512,
                             frame_hop=256,
                             ipd_index="0,1;0,2;0,3;0,4",
                             aug_prob=0.2)
    feats, stft, _ = transform(th.from_numpy(wav[None, ...]), None)
    assert feats.shape == th.Size(shape)
    assert th.sum(th.isnan(feats)) == 0
    assert stft.shape == th.Size([1, 5, 257, 366])
    assert transform.feats_dim == shape[-1]


@pytest.mark.parametrize("batch_size", [4])
@pytest.mark.parametrize("num_channels", [4, 8])
@pytest.mark.parametrize("num_bins", [257, 513])
@pytest.mark.parametrize("num_directions", [8, 16])
def test_fixed_beamformer(batch_size, num_channels, num_bins, num_directions):
    beamformer = FixedBeamformer(num_directions, num_channels, num_bins)
    num_frames = th.randint(50, 100, (1,)).item()
    inp_r = th.rand(batch_size, num_channels, num_bins, num_frames)
    inp_i = th.rand(batch_size, num_channels, num_bins, num_frames)
    inp_c = ComplexTensor(inp_r, inp_i)
    out_b = beamformer(inp_c)
    assert out_b.shape == th.Size(
        [batch_size, num_directions, num_bins, num_frames])
    out_b = beamformer(inp_c, beam=0)
    assert out_b.shape == th.Size([batch_size, num_bins, num_frames])


@pytest.mark.parametrize("num_bins", [257, 513])
@pytest.mark.parametrize("num_doas", [1, 8])
def test_df_transform(num_bins, num_doas):
    num_channels = 7
    batch_size = 4
    transform = DfTransform(num_bins=num_bins,
                            num_doas=num_doas,
                            af_index="1,0;2,0;3,0;4,0;5,0;6,0")
    num_frames = th.randint(50, 100, (1,)).item()
    phase = th.rand(batch_size, num_channels, num_bins, num_frames)
    doa = th.rand(batch_size)
    df = transform(phase, doa)
    if num_doas == 1:
        assert df.shape == th.Size([batch_size, num_bins, num_frames])
    else:
        assert df.shape == th.Size([batch_size, num_doas, num_bins, num_frames])


def debug_visualize_feature():
    transform = AsrTransform(feats="fbank-log-cmvn-delta",
                             frame_len=400,
                             frame_hop=160,
                             use_power=True,
                             pre_emphasis=0.97,
                             num_mels=80,
                             min_freq=20,
                             aug_prob=1,
                             norm_per_band=True,
                             aug_freq_args=(40, 1),
                             aug_time_args=(100, 1),
                             aug_mask_zero=False,
                             delta_as_channel=True)
    feats, _ = transform(th.from_numpy(egs1_wav[None, ...]), None)
    print(transform)
    from aps.plot import plot_feature
    plot_feature(feats[0, 1].numpy(), "egs")


def debug_speed_perturb():
    from aps.loader import write_audio
    speed_perturb = SpeedPerturbTransform(sr=16000)
    egs = read_audio("data/transform/egs1.wav", sr=16000, norm=False)
    # 12 x S
    batch = 12
    egs = th.repeat_interleave(th.from_numpy(egs[None, :]), batch, 0)
    egs_len = th.tensor([egs.shape[-1]] * batch, dtype=th.int64)
    egs_sp = speed_perturb(egs)
    for i in range(batch):
        write_audio(f"egs1-{i + 1}.wav", egs_sp[i].numpy(), norm=False)
    egs_len_sp = speed_perturb.output_length(egs_len)
    print(egs_len)
    print(egs_len_sp)


if __name__ == "__main__":
    debug_speed_perturb()
    # debug_visualize_feature()
