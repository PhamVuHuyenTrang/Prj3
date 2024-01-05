*Accepted at NeurIPS 2022*

Based on [https://github.com/aimagelab/mammoth](Mammoth)

## Citation

```
@inproceedings{bonicelli2022effectiveness,
    title={On the Effectiveness of Lipschitz-Driven Rehearsal in Continual Learning},
    author={Bonicelli, Lorenzo and Boschini, Matteo and Porrello, Angelo and Spampinato, Concetto and Calderara, Simone},
    booktitle = {Advances in Neural Information Processing Systems 35},
    year={2022}
}
```

## Example:

Command:

`python utils/main.py --dataset=seq-cifar100 --model=er_ace_lipschitz --n_epochs=50 --buffer_size=500 --lr=0.1 --load_cp=checkpoints/erace_pret_on_tinyr.pth --pre_epochs=200 --datasetS=tinyimgR --non_verbose`
