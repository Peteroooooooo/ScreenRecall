"""cao-code-01 受控场景：一个真的会出错的小 PyTorch 训练脚本。

不同参数触发不同的真实异常（形状不匹配、矩阵乘维度、batch 不一致）。
--simulate-oom 只是**打印**一段 CUDA out of memory 文本（Mac 上没有 CUDA），
采集记录里要如实写明它是脚本打印的，不是真实显存溢出。
"""
import argparse
import sys

import torch
from torch import nn


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch_size", type=int, default=16)
    ap.add_argument("--lr", type=float, default=3e-4)
    ap.add_argument("--epochs", type=int, default=3)
    ap.add_argument("--img", type=int, default=224, help="reshape 目标边长")
    ap.add_argument("--hidden", type=int, default=512)
    ap.add_argument("--label_bs", type=int, default=0, help="标签 batch，0 表示与输入相同")
    ap.add_argument("--simulate-oom", action="store_true")
    a = ap.parse_args()
    torch.manual_seed(a.batch_size * 7 + a.epochs)
    print(f"[config] batch_size={a.batch_size} lr={a.lr:g} epochs={a.epochs} img={a.img} hidden={a.hidden}")
    if a.simulate_oom:
        print("Epoch 1/%d" % a.epochs)
        print("torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 2.00 GiB "
              "(GPU 0; 7.79 GiB total capacity; 5.12 GiB already allocated; 1.31 GiB free) "
              "[printed by train.py --simulate-oom]", file=sys.stderr)
        return 1
    raw = torch.randn(a.batch_size, 3 * 200 * 200)            # 数据实际是 200x200
    model = nn.Sequential(nn.Flatten(), nn.Linear(3 * (a.img // 7) ** 2, a.hidden), nn.ReLU(),
                          nn.Linear(a.hidden, 10))
    opt = torch.optim.AdamW(model.parameters(), lr=a.lr)
    for ep in range(1, a.epochs + 1):
        x = raw.view(a.batch_size, 3, a.img, a.img)            # img!=200 时真实报错
        x = nn.functional.avg_pool2d(x, 7)
        y = torch.randint(0, 10, (a.label_bs or a.batch_size,))
        loss = nn.functional.cross_entropy(model(x), y)
        opt.zero_grad()
        loss.backward()
        opt.step()
        print(f"Epoch {ep}/{a.epochs}  loss={loss.item():.4f}  lr={a.lr:g}")
    print("done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
