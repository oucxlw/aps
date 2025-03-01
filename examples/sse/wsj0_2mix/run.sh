#!/usr/bin/env bash

# Copyright 2019 Jian Wu
# License: Apache 2.0 (http://www.apache.org/licenses/LICENSE-2.0)

set -eu

stage="1-3"

wsj0_2mix_dir=/scratch/jwu/wsj0_2mix
dataset="wsj0_2mix"

exp="1a"
gpu=0
seed=777
epochs=100
tensorboard=false
batch_size=8
num_workers=4
eval_interval=-1
save_interval=-1
prog_interval=100

metric=sisnr

. ./utils/parse_options.sh || exit 1;

beg=$(echo $stage | awk -F '-' '{print $1}')
end=$(echo $stage | awk -F '-' '{print $2}')
[ -z $end ] && end=$beg

data_dir=data/$dataset

if [ $end -ge 1 ] && [ $beg -le 1 ]; then
  echo "Stage 1: preparing data ..."
  for subdir in "tr" "tt" "cv"; do
    [ ! -d $wsj0_2mix_dir/wav8k/min/$subdir ] && \
      echo "$wsj0_2mix_dir/wav8k/min/$subdir not exists, exit ..." && exit 1
  done
  ./local/format_data.sh $wsj0_2mix_dir/wav8k/min $data_dir
fi

cpt_dir=exp/$dataset/$exp
if [ $end -ge 2 ] && [ $beg -le 2 ]; then
  echo "Stage 2: training SS model ..."
  ./scripts/train.sh \
    --gpu $gpu \
    --seed $seed \
    --epochs $epochs \
    --batch-size $batch_size \
    --num-workers $num_workers \
    --eval-interval $eval_interval \
    --save-interval $save_interval \
    --prog-interval $prog_interval \
    --tensorboard $tensorboard \
    ss $dataset $exp
  echo "$0: Train model done under $cpt_dir"
fi

if [ $end -ge 3 ] && [ $beg -le 3 ]; then
  echo "Stage 3: run speech separation ..."
  # generate separation audio under $cpt_dir/bss
  for name in cv tt; do
    ./cmd/separate.py \
      --checkpoint $cpt_dir \
      --sr 8000 \
      --device-id $gpu \
      $data_dir/$name/mix.scp \
      $cpt_dir/bss_$name
  done
fi

if [ $end -ge 4 ] && [ $beg -le 4 ]; then
  echo "Stage 4: evaluate ..."
  for name in cv tt; do
    for index in 1 2; do
      find $cpt_dir/bss_$name -name "*.wav" | \
        awk -v ch=$index -F '/' '{printf("%s sox %s -t wav - remix %d |\n", $NF, $0, ch)}' | \
        sed "s:.wav::" > $cpt_dir/bss_$name/spk${index}.scp
    done
    ./cmd/compute_ss_metric.py --sr 8000 --metric $metric \
      $data_dir/$name/spk1.scp,$data_dir/$name/spk2.scp \
      $cpt_dir/bss_$name/spk1.scp,$cpt_dir/bss_$name/spk2.scp
  done
fi
