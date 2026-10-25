"""真实 Terminal 窗口里的代码 / 报错类受控任务（cao 正式采集，capture_real_mac.py terminal2 使用）。

每个任务：setup(work) 在中性工作目录里生成测试文件；STEPS[task] 是逐步真实运行的命令。命令输出原样显示在
真实 Terminal 窗口里，提示符由脚本打印。每步都换内容（不同参数 / 文件 / 报错），不靠重复运行同一命令凑数。
文件名、目录名、输出里都不出现任务编号或场景名（会变成类别线索）。

step 字段：cmd 命令；size (行, 列) 截图前用 CSI 8 调整窗口；keep=True 表示不清屏、接着上一步输出；
lang 语言；var 画面变化说明；facts 本步画面里出现的事实编号（植入事实只是草稿，查询真值由人确认）。
"""
from __future__ import annotations

import textwrap
from pathlib import Path

W = (36, 120)   # 默认窗口
M = (30, 100)
S = (24, 90)


def _w(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")


# ---------------------------------------------------------------- code-01（续采，原 5 张后再 20 张）

def setup_01(work: Path) -> None:
    import shutil
    shutil.copy(Path(__file__).with_name("code01_train.py"), work / "train.py")


def steps_01():
    runs = [
        ("--batch_size 8 --img 200 --epochs 4", W, "batch 8 训练成功 4 个 epoch"),
        ("--batch_size 32 --img 192", W, "img=192 形状报错 [32, 3, 192, 192]"),
        ("--batch_size 32 --img 200 --lr 1e-3 --epochs 5", W, "lr 1e-3 训练 5 个 epoch"),
        ("--batch_size 16 --img 200 --hidden 1024 --epochs 2", M, "hidden 1024；窗口 100x30"),
        ("--batch_size 24 --img 200 --label_bs 16", M, "标签 batch 16 与输入 24 不一致"),
        ("--batch_size 64 --img 200 --lr 5e-5 --epochs 3", W, "batch 64、lr 5e-5"),
        ("--simulate-oom --batch_size 256", S, "batch 256 打印 OOM 文本（脚本打印，非真实）；窗口 90x24"),
        ("--batch_size 12 --img 210", W, "img=210 形状报错 [12, 3, 210, 210]"),
        ("--batch_size 4 --img 200 --hidden 128 --epochs 6", W, "batch 4、hidden 128、6 个 epoch"),
        ("--batch_size 48 --img 200 --lr 2e-4 --label_bs 47", M, "标签 batch 47 与输入 48 不一致"),
    ]
    out = []
    for i, (args, size, var) in enumerate(runs):
        out.append(dict(cmd=f"python train.py {args}", size=size, keep=False, lang="en", var=var,
                        facts="cao-code-01#3" if "--img 192" in args else ""))
        # 每次运行后接一条不同的辅助命令，画面内容不同
        aux = [
            ("python -c \"import torch; print('torch', torch.__version__, 'mps', torch.backends.mps.is_available())\"",
             "查看 torch 版本与 MPS"),
            ("grep -n 'add_argument' train.py", "列出脚本参数定义"),
            ("python train.py --help", "参数帮助"),
            ("sed -n '23,40p' train.py", "查看训练循环源码"),
            ("python -c \"import torch; x=torch.randn(16,3,200,200); print(x.shape, x.numel())\"", "核对张量大小"),
            ("ls -la", "列目录"),
            ("python -c \"import platform,sys; print(platform.platform()); print(sys.version)\"", "系统与 Python 版本"),
            ("wc -l train.py && head -12 train.py", "脚本开头"),
            ("python -c \"import torch; print(torch.randn(3,4).softmax(-1))\"", "softmax 小例子"),
            ("echo 'next: try --img 200 --batch_size 16' && date '+%Y-%m-%d %H:%M'", "打印下一步备忘与时间"),
        ][i]
        out.append(dict(cmd=aux[0], size=size, keep=True, lang="en", var=f"在上一条输出下方运行：{aux[1]}"))
    return out


# ---------------------------------------------------------------- code-02 依赖安装失败

def setup_02(work: Path) -> None:
    _w(work / "requirements-a.txt", """
        flask==3.0.0
        werkzeug<3
    """)
    _w(work / "requirements-b.txt", """
        pandas==2.2.2
        numpy<1.22
    """)
    _w(work / "requirements-c.txt", """
        requests==2.32.3
        urllib3<1.21
    """)
    _w(work / "requirements-d.txt", """
        scikit-learn==1.5.1
        scipy<1.3
    """)
    _w(work / "app.py", """
        import yamlx  # 故意写错的包名
        print("loaded")
    """)


def steps_02():
    uv = "uv pip install --dry-run --python .venv/bin/python"
    s = [
        ("uv venv -q .venv && .venv/bin/python -V", "新建空虚拟环境"),
        ("cat requirements-a.txt", "查看依赖文件 a"),
        (f"{uv} -r requirements-a.txt", "flask 3.0.0 与 werkzeug<3 冲突"),
        ("cat requirements-b.txt", "查看依赖文件 b"),
        (f"{uv} -r requirements-b.txt", "pandas 2.2.2 与 numpy<1.22 冲突"),
        (f"{uv} -r requirements-c.txt", "requests 与 urllib3<1.21 冲突"),
        (f"{uv} -r requirements-d.txt", "scikit-learn 与 scipy<1.3 冲突"),
        (f"{uv} 'torch==0.0.1'", "不存在的版本"),
        (f"{uv} 'definitely-not-a-real-pkg-4127'", "不存在的包"),
        (".venv/bin/python app.py", "ModuleNotFoundError: yamlx"),
        (".venv/bin/python -c 'import numpy'", "空环境里 import numpy 失败"),
        (f"{uv} 'flask==3.0.0'", "单独安装 flask 3.0.0 的解析结果"),
        (f"{uv} 'pandas==2.2.2'", "pandas 2.2.2 解析结果"),
        (f"{uv} 'numpy==1.21.6' 'python-dateutil>=2.8'", "旧版 numpy 在 Python 3.12 上的解析"),
        ("uv pip list --python .venv/bin/python; ls -la .venv/lib/python3.12/site-packages", "空环境包列表与 site-packages"),
        (f"{uv} 'black==24.4.2' 'click<8'", "black 与 click<8 冲突"),
        (f"{uv} 'pydantic==2.7.0' 'pydantic-core==2.0.0'", "pydantic 与 pydantic-core 版本冲突"),
        (f"{uv} 'jupyterlab==4.2.0' 'tornado<6'", "jupyterlab 与 tornado<6 冲突"),
        ("uv --version && which uv", "uv 版本"),
        (f"{uv} 'fastapi==0.111.0' 'starlette==0.20.0'", "fastapi 与 starlette 冲突"),
        (f"{uv} 'matplotlib==3.9.0' 'pillow<8'", "matplotlib 与 pillow<8 冲突"),
        (".venv/bin/python -m pip --version", "venv 里没有 pip"),
        (f"{uv} 'httpx==0.27.0' 'httpcore<0.10'", "httpx 与 httpcore 冲突"),
        (f"{uv} 'transformers==4.41.0' 'tokenizers<0.10'", "transformers 与 tokenizers 冲突"),
        ("printf 'flask==3.0.0\\nwerkzeug>=3.0.0\\n' > requirements-fixed.txt && "
         f"cat requirements-fixed.txt && {uv} -r requirements-fixed.txt", "改正后的依赖可以解析"),
    ]
    sizes = [W, W, W, W, W, M, M, W, S, W, S, W, W, M, S, W, W, M, S, W, W, S, M, W, W]
    facts = {2: "cao-code-02#1", 4: "cao-code-02#2"}
    return [dict(cmd=c, size=sizes[i], keep=(i in (1, 3)), lang="en", var=v, facts=facts.get(i, ""))
            for i, (c, v) in enumerate(s)]


# ---------------------------------------------------------------- code-04 pytest 失败

def setup_04(work: Path) -> None:
    _w(work / "pricing.py", """
        def apply_discount(price, pct):
            return round(price * (1 - pct / 100), 1)

        def bulk_total(items):
            return sum(q * p for q, p in items)

        def parse_sku(s):
            vendor, num = s.split("-")
            return vendor.upper(), int(num)
    """)
    _w(work / "test_pricing.py", """
        import pytest
        from pricing import apply_discount, bulk_total, parse_sku

        @pytest.mark.parametrize("price,pct,expected", [
            (100, 10, 90.0), (19.99, 15, 16.99), (250, 0, 250), (80, 12.5, 70.0),
        ])
        def test_apply_discount(price, pct, expected):
            assert apply_discount(price, pct) == expected

        def test_bulk_total():
            assert bulk_total([(2, 3.5), (1, 10)]) == 17.0

        def test_bulk_total_empty():
            assert bulk_total([]) == 0

        def test_parse_sku():
            assert parse_sku("acme-0042") == ("ACME", 42)

        def test_parse_sku_bad():
            assert parse_sku("acme0042") == ("ACME", 42)

        def test_dict_diff():
            got = {"sku": "ACME-42", "qty": 3, "price": 12.5, "currency": "SGD"}
            assert got == {"sku": "ACME-42", "qty": 4, "price": 12.5, "currency": "SGD"}

        def test_list_diff():
            assert [1, 2, 3, 5, 8] == [1, 2, 3, 5, 8, 13]
    """)


def steps_04():
    py = "python -m pytest -p no:cacheprovider"
    s = [
        (f"{py} -q", "全部用例运行，失败汇总"),
        (f"{py} -q -k discount", "只跑折扣参数化用例"),
        (f"{py} -q 'test_pricing.py::test_apply_discount[19.99-15-16.99]'", "单个参数化用例编号"),
        (f"{py} -q -k dict_diff", "字典断言 diff"),
        (f"{py} -q -k list_diff", "列表断言 diff"),
        (f"{py} -q -k parse_sku_bad", "ValueError: not enough values to unpack"),
        (f"{py} -rA -q -k bulk", "bulk 用例全部通过 + 汇总"),
        (f"{py} --tb=line -q", "--tb=line 简短回溯"),
        (f"{py} --tb=no -q -rf", "只列失败用例名"),
        (f"{py} -v -k discount", "-v 逐个参数化用例结果"),
        (f"{py} -x -q", "-x 遇第一个失败停止"),
        (f"{py} --co -q", "只收集用例列表"),
        (f"{py} -q -k 'not diff and not bad'", "排除 diff 与 bad 后的结果"),
        ("sed -n '1,12p' test_pricing.py", "查看参数化定义"),
        ("sed -i '' 's/(19.99, 15, 16.99)/(19.99, 15, 17.0)/' test_pricing.py && " + f"{py} -q -k discount",
         "改断言期望值 16.99→17.0 后重跑"),
        ("sed -i '' 's/\"qty\": 4/\"qty\": 3/' test_pricing.py && " + f"{py} -q -k dict_diff", "修正 qty 后重跑字典用例"),
        (f"{py} -q --durations=3", "最慢用例耗时"),
        ("sed -i '' 's/, 13\\]/]/' test_pricing.py && " + f"{py} -q -k list_diff", "修正列表期望后重跑"),
        (f"{py} -q -k parse --tb=short", "parse 用例 --tb=short"),
        ("cat pricing.py", "查看被测函数"),
        ("sed -i '' 's/round(price \\* (1 - pct \\/ 100), 1)/round(price * (1 - pct \\/ 100), 2)/' pricing.py && "
         f"{py} -q -k discount", "round 改为 2 位小数后的新失败"),
        (f"{py} -q -k 'discount and 80'", "80-12.5 用例"),
        (f"{py} -q -rA", "最终全部结果与汇总"),
        (f"{py} --tb=long -q -k parse_sku_bad", "--tb=long 完整回溯"),
        (f"{py} -q 2>&1 | tail -6", "只看最后的失败数统计"),
    ]
    sizes = [W, W, M, W, W, W, M, W, S, W, W, M, S, S, W, W, M, W, W, S, W, M, W, W, S]
    facts = {3: "cao-code-04#1", 2: "cao-code-04#2"}
    return [dict(cmd=c, size=sizes[i], keep=False, lang="en", var=v, facts=facts.get(i, ""))
            for i, (c, v) in enumerate(s)]


# ---------------------------------------------------------------- code-05 配置文件解析

def setup_05(work: Path) -> None:
    _w(work / "check_config.py", """
        import json, sys, yaml
        REQUIRED = {"model_name": str, "batch_size": int, "max_epochs": int, "learning_rate": float}
        path = sys.argv[1]
        with open(path, encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) if path.endswith((".yaml", ".yml")) else json.load(fh)
        print(f"[loaded] {path}")
        for k, t in REQUIRED.items():
            if k not in cfg:
                raise SystemExit(f"ConfigError: missing required field '{k}' in {path}")
            if not isinstance(cfg[k], t):
                raise SystemExit(f"ConfigError: field '{k}' expects {t.__name__}, got {type(cfg[k]).__name__} ({cfg[k]!r})")
        print(json.dumps(cfg, indent=2, ensure_ascii=False))
    """)
    _w(work / "configs/base.yaml", """
        model_name: vit-small-patch16-224
        batch_size: 32
        max_epochs: 40
        learning_rate: 0.0003
        scheduler: {name: cosine, warmup_steps: 500}
        data: {root: ./data/flowers, num_workers: 4}
    """)
    _w(work / "configs/bad_indent.yaml", """
        model_name: resnet50
        batch_size: 64
          max_epochs: 90
        learning_rate: 0.1
    """)
    _w(work / "configs/bad_type.yaml", """
        model_name: convnext-tiny
        batch_size: "sixteen"
        max_epochs: 30
        learning_rate: 0.0005
    """)
    _w(work / "configs/missing.yaml", """
        model_name: efficientnet-b0
        batch_size: 128
        learning_rate: 0.002
    """)
    _w(work / "configs/tab.yaml", "model_name: mobilenet-v3\n\tbatch_size: 256\n")
    _w(work / "configs/train.json", """
        {"model_name": "bert-base-chinese", "batch_size": 24, "max_epochs": 5, "learning_rate": 2e-5,}
    """)
    _w(work / "configs/ok.json", """
        {"model_name": "whisper-tiny", "batch_size": 8, "max_epochs": 12, "learning_rate": 0.0001,
         "language": "zh"}
    """)
    _w(work / "configs/lr_str.yaml", """
        model_name: clip-vit-b32
        batch_size: 48
        max_epochs: 20
        learning_rate: 3e-4
    """)
    _w(work / "configs/dup.yaml", """
        model_name: yolov8n
        batch_size: 16
        batch_size: 32
        max_epochs: 100
        learning_rate: 0.01
    """)


def steps_05():
    py = "python check_config.py"
    s = [
        ("cat configs/base.yaml", "查看基础配置"),
        (f"{py} configs/base.yaml", "基础配置通过，打印 JSON"),
        ("cat -n configs/bad_indent.yaml", "带行号查看缩进错误的配置"),
        (f"{py} configs/bad_indent.yaml", "YAML 缩进错误 mapping values are not allowed"),
        (f"{py} configs/bad_type.yaml", "batch_size 类型错误"),
        (f"{py} configs/missing.yaml", "缺少 max_epochs"),
        ("cat -et configs/tab.yaml", "显示 tab 字符"),
        (f"{py} configs/tab.yaml", "tab 缩进报错"),
        ("cat configs/train.json", "查看 JSON 配置"),
        (f"{py} configs/train.json", "JSON 尾逗号解析错误"),
        (f"{py} configs/ok.json", "JSON 配置通过"),
        (f"{py} configs/lr_str.yaml", "3e-4 被 YAML 读成字符串"),
        ("cat configs/lr_str.yaml", "查看学习率写法"),
        (f"{py} configs/dup.yaml", "重复键 batch_size 被静默覆盖"),
        ("ls -la configs", "配置目录"),
        ("grep -rn 'model_name' configs", "所有配置的模型名"),
        ("grep -rn 'batch_size' configs", "所有配置的 batch_size"),
        ("sed -i '' 's/^  max_epochs/max_epochs/' configs/bad_indent.yaml && " + f"{py} configs/bad_indent.yaml",
         "修正缩进后通过"),
        ("sed -i '' 's/\"sixteen\"/16/' configs/bad_type.yaml && " + f"{py} configs/bad_type.yaml", "修正类型后通过"),
        ("printf 'max_epochs: 50\\n' >> configs/missing.yaml && " + f"{py} configs/missing.yaml", "补上字段后通过"),
        ("sed -i '' 's/3e-4/3.0e-4/' configs/lr_str.yaml && " + f"{py} configs/lr_str.yaml", "改成 3.0e-4 后通过"),
        ("diff configs/base.yaml configs/lr_str.yaml", "两份配置的差异"),
        ("python -c \"import yaml; print(yaml.safe_load('a: [1, 2'))\"", "未闭合列表的报错"),
        ("python -c \"import yaml; print(yaml.safe_load(open('configs/base.yaml'))['scheduler'])\"",
         "读取嵌套字段"),
        ("sed -i '' 's/,}/}/' configs/train.json && " + f"{py} configs/train.json", "去掉尾逗号后通过"),
    ]
    sizes = [S, W, S, W, M, M, S, W, S, W, W, M, S, W, S, M, M, W, W, W, W, S, W, S, W]
    facts = {3: "cao-code-05#1", 1: "cao-code-05#2"}
    return [dict(cmd=c, size=sizes[i], keep=False, lang="en", var=v, facts=facts.get(i, ""))
            for i, (c, v) in enumerate(s)]


# ---------------------------------------------------------------- code-06 git

GIT = ("git -c user.name='Lin Wei' -c user.email=lin.wei@example.com -c init.defaultBranch=main "
       "-c advice.detachedHead=true -c color.ui=always")


def setup_06(work: Path) -> None:
    _w(work / "make_repos.sh", f"""
        set -e
        g() {{ {GIT} "$@"; }}
        rm -rf origin.git alpha beta
        git init -q --bare -b main origin.git
        g clone -q origin.git alpha 2>/dev/null
        cd alpha
        printf 'batch_size: 16\\nlr: 0.001\\n' > params.yaml
        printf '# Flower classifier\\n' > README.md
        g add . && g commit -q -m 'init: params and readme'
        g push -q origin main
        g checkout -q -b feature/augment-mixup
        printf 'batch_size: 32\\nlr: 0.001\\naugment: mixup\\n' > params.yaml
        g commit -qam 'feat: enable mixup, batch 32'
        g checkout -q main
        printf 'batch_size: 24\\nlr: 0.0005\\n' > params.yaml
        g commit -qam 'tune: batch 24, lr 5e-4'
        cd ..
        g clone -q origin.git beta 2>/dev/null
        cd beta && printf 'v2\\n' >> README.md && g commit -qam 'docs: beta note' && g push -q origin main
    """)


def steps_06():
    a = f"cd alpha && {GIT}"
    s = [
        ("zsh make_repos.sh && ls", "生成示例仓库"),
        (f"{a} log --oneline --graph --all", "所有分支提交图"),
        (f"{a} branch -vv", "分支列表"),
        (f"{a} status", "main 上的状态"),
        (f"{a} merge feature/augment-mixup", "merge 冲突 params.yaml"),
        (f"{a} status", "冲突中的状态"),
        ("cd alpha && cat params.yaml", "冲突标记内容"),
        (f"{a} diff", "冲突 diff"),
        (f"{a} merge --abort && {GIT} status -sb", "放弃合并"),
        (f"{a} push origin main", "push rejected（远端有新提交）"),
        (f"{a} fetch -q && {GIT} log --oneline --graph --all", "fetch 后的分叉"),
        (f"{a} pull --no-rebase --no-edit origin main 2>&1 | tail -4; {GIT} log --oneline -4", "pull 合并远端"),
        (f"{a} checkout HEAD~1", "detached HEAD 提示"),
        (f"{a} status", "detached 状态"),
        (f"{a} checkout -q main && {GIT} rebase feature/augment-mixup", "rebase 冲突"),
        (f"{a} rebase --abort && {GIT} log --oneline -3", "放弃 rebase"),
        (f"{a} checkout -b hotfix/lr-warmup && {GIT} branch", "新建 hotfix 分支"),
        (f"{a} checkout does-not-exist", "不存在的分支"),
        (f"{a} show --stat HEAD", "最新提交详情"),
        (f"{a} log -p -1 feature/augment-mixup", "feature 分支补丁"),
        (f"{a} stash list; printf 'x\\n' >> README.md; {GIT} stash; {GIT} stash list", "stash"),
        (f"{a} remote -v && {GIT} ls-remote origin", "远端信息"),
        (f"{a} tag v0.1 main && {GIT} tag -n && {GIT} push origin nonexistent-ref", "推送不存在的引用报错"),
        ("cd beta && " + GIT + " log --oneline --decorate", "另一个克隆的历史"),
        (f"{a} blame params.yaml", "blame 每行作者"),
    ]
    sizes = [S, W, S, M, W, W, S, W, S, W, W, W, M, M, W, S, S, S, M, W, M, W, W, S, M]
    facts = {4: "cao-code-06#1", 9: "cao-code-06#2"}
    return [dict(cmd=c, size=sizes[i], keep=False, lang="en", var=v, facts=facts.get(i, ""))
            for i, (c, v) in enumerate(s)]


# ---------------------------------------------------------------- code-07 curl

def setup_07(work: Path) -> None:
    pass


def steps_07():
    h = "https://httpbin.org"
    s = [
        (f"curl -s -i {h}/status/401", "401 响应头"),
        (f"curl -s -i {h}/status/429", "429 Too Many Requests"),
        (f"curl -s -i {h}/status/500", "500 内部错误"),
        (f"curl -s -v {h}/status/404 2>&1 | head -30", "-v 详细握手与 404"),
        (f"curl -s {h}/basic-auth/demo/s3cret -u demo:wrong -i", "basic auth 失败"),
        (f"curl -s {h}/basic-auth/demo/s3cret -u demo:s3cret", "basic auth 成功 JSON"),
        (f"curl -s {h}/bearer -i", "缺少 bearer token 401"),
        (f"curl -s {h}/bearer -H 'Authorization: Bearer test-token-7731'", "bearer 成功"),
        (f"curl -s {h}/headers -H 'X-Request-Id: req-20261020-0042'", "回显请求头"),
        (f"curl -s {h}/anything/orders/A-5531 -X POST -H 'Content-Type: application/json' "
         "-d '{\"qty\": 2, \"sku\": \"KB-87\"}'", "POST JSON 回显"),
        (f"curl -s -o /dev/null -w 'code=%{{http_code}} time=%{{time_total}}s\\n' {h}/delay/2", "只看状态码与耗时"),
        (f"curl -s -i {h}/status/418", "418 I'm a teapot"),
        (f"curl -s -i {h}/status/503", "503"),
        (f"curl -s {h}/json", "示例 JSON 文档"),
        (f"curl -s -i {h}/redirect/3 | head -12", "302 重定向头"),
        (f"curl -s -L {h}/redirect/2", "-L 跟随重定向"),
        (f"curl -s '{h}/get?city=Singapore&units=metric'", "查询参数回显"),
        (f"curl -s -i -X DELETE {h}/status/405", "405 方法不允许"),
        (f"curl -s -i '{h}/response-headers?X-RateLimit-Remaining=0'", "限流响应头"),
        (f"curl -s --max-time 1 {h}/delay/5; echo \"exit=$?\"", "超时 exit 28"),
        ("curl -s https://no-such-host.invalid/api; echo \"exit=$?\"", "DNS 失败 exit 6"),
        (f"curl -s {h}/uuid; curl -s {h}/ip", "uuid 与 ip"),
        (f"curl -s {h}/user-agent -A 'screenrecall-test/0.1'", "自定义 UA"),
        (f"curl -s -i {h}/status/422", "422"),
        (f"curl -s -L -c - '{h}/cookies/set?session=demo-cookie-001'", "Set-Cookie 与 cookie jar"),
    ]
    sizes = [S, S, S, W, M, S, S, S, M, W, S, S, S, W, M, M, M, S, M, S, S, S, S, S, W]
    facts = {1: "cao-code-07#1", 6: "cao-code-07#2"}
    return [dict(cmd=c, size=sizes[i], keep=(i in (2, 12, 23)), lang="en", var=v, facts=facts.get(i, ""))
            for i, (c, v) in enumerate(s)]


# ---------------------------------------------------------------- code-08 sqlite

def setup_08(work: Path) -> None:
    _w(work / "schema.sql", """
        PRAGMA foreign_keys = ON;
        CREATE TABLE customers(id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, city TEXT);
        CREATE TABLE orders(order_no TEXT PRIMARY KEY, customer_id INTEGER NOT NULL REFERENCES customers(id),
                            amount REAL CHECK (amount > 0), placed TEXT,
                            CONSTRAINT uq_order_customer_day UNIQUE (customer_id, placed));
        INSERT INTO customers VALUES (1,'mei@example.com','Singapore'),(2,'ravi@example.com','Penang'),
                                     (3,'anna@example.com','Zurich');
        INSERT INTO orders VALUES ('SO-10231',1,129.90,'2026-10-01'),('SO-10232',2,48.00,'2026-10-02'),
                                  ('SO-10233',1,15.50,'2026-10-03'),('SO-10234',3,999.00,'2026-10-03');
    """)


def steps_08():
    q = "sqlite3 shop.db"
    s = [
        ("rm -f shop.db && sqlite3 shop.db < schema.sql && ls -la shop.db", "建示例库"),
        (f"{q} '.tables'", "表列表"),
        (f"{q} '.schema orders'", "orders 表结构"),
        (f"{q} -box 'SELECT * FROM orders'", "box 模式订单表"),
        (f"{q} 'SELECT * FROM orders'", "默认 list 模式"),
        (f"{q} \"INSERT INTO customers VALUES (4,'mei@example.com','Ipoh')\"", "UNIQUE constraint failed: customers.email"),
        (f"{q} \"PRAGMA foreign_keys=ON; INSERT INTO orders VALUES ('SO-10235',9,10,'2026-10-04')\"",
         "FOREIGN KEY constraint failed"),
        (f"{q} \"INSERT INTO orders VALUES ('SO-10236',2,-5,'2026-10-05')\"", "CHECK constraint failed"),
        (f"{q} \"INSERT INTO orders VALUES ('SO-10237',1,20,'2026-10-01')\"", "复合 UNIQUE 约束失败"),
        (f"{q} 'SELEC * FROM orders'", "语法错误 near SELEC"),
        (f"{q} 'SELECT amount FROM order'", "near order 语法错误"),
        (f"{q} 'SELECT nope FROM orders'", "no such column"),
        (f"{q} -box 'SELECT c.city, COUNT(*) n, SUM(o.amount) total FROM orders o JOIN customers c "
         "ON c.id=o.customer_id GROUP BY c.city'", "按城市汇总"),
        (f"{q} -column -header 'SELECT order_no, amount FROM orders ORDER BY amount DESC'", "column 模式排序"),
        (f"{q} -json 'SELECT * FROM customers'", "json 输出"),
        (f"{q} -csv -header 'SELECT * FROM orders'", "csv 输出"),
        (f"{q} -markdown 'SELECT * FROM customers'", "markdown 输出"),
        (f"{q} \"UPDATE orders SET amount = 0 WHERE order_no='SO-10232'\"", "更新违反 CHECK"),
        (f"{q} \"INSERT INTO customers(email, city) VALUES (NULL, 'Kuching')\"", "NOT NULL constraint failed"),
        (f"{q} 'PRAGMA integrity_check; PRAGMA foreign_key_list(orders);'", "完整性与外键"),
        (f"{q} -box \"SELECT * FROM orders WHERE placed BETWEEN '2026-10-02' AND '2026-10-03'\"", "日期区间"),
        (f"{q} 'SELECT COUNT(*) FROM missing_table'", "no such table"),
        (f"{q} -box 'EXPLAIN QUERY PLAN SELECT * FROM orders WHERE customer_id=1'", "查询计划"),
        (f"{q} -box \"SELECT email, city FROM customers WHERE email LIKE '%@example.com'\"", "LIKE 查询"),
        (f"{q} '.dump orders' | head -20", "dump"),
    ]
    sizes = [S, S, M, W, S, W, W, W, W, S, S, S, W, M, M, S, M, W, S, M, W, S, W, W, W]
    facts = {5: "cao-code-08#1", 3: "cao-code-08#2"}
    return [dict(cmd=c, size=sizes[i], keep=(i in (4, 10, 21)), lang="en", var=v, facts=facts.get(i, ""))
            for i, (c, v) in enumerate(s)]


# ---------------------------------------------------------------- code-09 clang / make

def setup_09(work: Path) -> None:
    _w(work / "undeclared.c", """
        #include <stdio.h>
        int main(void) {
            int total = 0;
            for (int i = 0; i < 10; i++) total += i;
            printf("%d\\n", totl);
            return 0;
        }
    """)
    _w(work / "warn.c", """
        #include <stdio.h>
        int scale(int x) { int unused = 3; if (x > 0) return x * 2; }
        int main(void) { unsigned u = 7; int s = -1; if (s < u) printf("odd\\n"); printf("%s\\n", 42); return 0; }
    """)
    _w(work / "segv.c", """
        #include <stdio.h>
        #include <string.h>
        struct sensor { char name[16]; double *readings; };
        int main(void) {
            struct sensor s; strcpy(s.name, "thermo-07"); s.readings = NULL;
            printf("reading %s...\\n", s.name);
            printf("%f\\n", s.readings[3]);
            return 0;
        }
    """)
    _w(work / "link.c", """
        double kalman_step(double x, double z);
        int main(void) { return (int)kalman_step(1.0, 2.0); }
    """)
    _w(work / "types.c", """
        #include <stdlib.h>
        typedef struct { int id; } job_t;
        int main(void) { job_t *j = malloc(sizeof *j); j->id = "J-118"; char *p = j; free(j); return p == 0; }
    """)
    _w(work / "ok.c", """
        #include <stdio.h>
        int main(int argc, char **argv) { printf("args=%d first=%s\\n", argc, argc > 1 ? argv[1] : "-"); return 0; }
    """)
    _w(work / "Makefile", """
        CC=clang
        CFLAGS=-Wall -Wextra -O0 -g
        all: ok segv
        ok: ok.c
        \t$(CC) $(CFLAGS) -o $@ $<
        segv: segv.c
        \t$(CC) $(CFLAGS) -o $@ $<
        broken: undeclared.c
        \t$(CC) $(CFLAGS) -o $@ $<
        clean:
        \trm -f ok segv broken a.out
    """.replace("\\t", "\t"))


def steps_09():
    s = [
        ("cat -n undeclared.c", "源码带行号"),
        ("clang -Wall undeclared.c", "use of undeclared identifier 'totl'"),
        ("clang -Wall -fno-color-diagnostics undeclared.c", "无色诊断输出"),
        ("clang -Wall -Wextra warn.c -o warn", "多条警告与行号"),
        ("./warn; echo \"exit=$?\"", "运行带警告的程序（printf %s 传整数导致崩溃）"),
        ("clang -g segv.c -o segv && ./segv; echo \"exit=$?\"", "Segmentation fault"),
        ("cat -n segv.c", "段错误源码"),
        ("clang link.c", "链接错误 undefined symbol _kalman_step"),
        ("clang -Wall types.c", "类型不兼容警告"),
        ("make clean && make", "make 编译成功"),
        ("make broken", "make 编译失败"),
        ("./ok alpha beta", "运行 ok"),
        ("clang -fsanitize=address -g segv.c -o segv-asan && ./segv-asan 2>&1 | head -20", "ASan 报告"),
        ("clang -Werror -Wall warn.c -o warn", "-Werror 把警告变错误"),
        ("clang --version", "编译器版本"),
        ("clang -E ok.c | tail -5", "预处理输出"),
        ("clang -S -O2 ok.c -o - | head -25", "汇编输出"),
        ("make -n", "make 演练"),
        ("make nothing-here", "不存在的目标"),
        ("sed -i '' 's/totl/total/' undeclared.c && clang -Wall undeclared.c -o fixed && ./fixed", "修正后运行"),
        ("nm ok | head", "符号表"),
        ("clang -std=c89 -pedantic -Wall -c warn.c -o /dev/null", "c89 pedantic 警告"),
        ("otool -L ok", "动态库依赖"),
        ("clang -Wall -Wshadow -c ok.c -o ok.o; ls -la *.o", "只编译不链接"),
        ("make clean && ls", "清理"),
    ]
    sizes = [S, W, W, W, S, M, S, W, W, M, M, S, W, W, S, S, W, S, S, M, S, M, S, M, S]
    facts = {1: "cao-code-09#1", 5: "cao-code-09#2"}
    return [dict(cmd=c, size=sizes[i], keep=(i in (4, 11, 20)), lang="en", var=v, facts=facts.get(i, ""))
            for i, (c, v) in enumerate(s)]


# ---------------------------------------------------------------- code-10 调试（pdb）

def setup_10(work: Path) -> None:
    _w(work / "billing.py", """
        class BillingError(Exception):
            def __init__(self, code, msg):
                super().__init__(f"[{code}] {msg}")
                self.code = code

        RATES = {"basic": 9.9, "pro": 29.0, "team": 79.0}

        def invoice(plan, seats, coupon=None):
            rate = RATES[plan]
            subtotal = rate * seats
            discount = 0.15 if coupon == "NUS15" else 0.0
            if seats > 50:
                raise BillingError("E-4127", f"seat limit exceeded: {seats} > 50 for plan {plan}")
            total = round(subtotal * (1 - discount), 2)
            return {"plan": plan, "seats": seats, "subtotal": subtotal, "total": total}

        if __name__ == "__main__":
            import sys
            plan, seats = sys.argv[1], int(sys.argv[2])
            print(invoice(plan, seats, sys.argv[3] if len(sys.argv) > 3 else None))
    """)


def steps_10():
    pdb = "printf '{}' | python -m pdb billing.py {}"
    s = [
        ("cat -n billing.py", "源码带行号"),
        ("python billing.py pro 3", "正常运行"),
        ("python billing.py team 64", "自定义错误码 E-4127 回溯"),
        ("python billing.py enterprise 2", "KeyError: 'enterprise'"),
        (pdb.format("b 12\\nc\\np rate, seats, subtotal\\nc\\n", "pro 3 NUS15"), "断点 12 行查看变量"),
        (pdb.format("b 12\\nc\\npp locals()\\nq\\n", "team 7"), "pp locals() 全部局部变量"),
        (pdb.format("b 14\\nc\\nw\\nq\\n", "team 64"), "调用栈 where"),
        (pdb.format("c\\nw\\np seats\\nq\\n", "team 64"), "异常后 post-mortem"),
        (pdb.format("b invoice\\nc\\nn\\nn\\nn\\np subtotal\\nq\\n", "basic 12"), "逐行 next"),
        (pdb.format("b 15\\nc\\np discount\\np total\\nq\\n", "pro 10 NUS15"), "折扣变量"),
        (pdb.format("l 8,16\\nq\\n", "pro 1"), "list 源码"),
        (pdb.format("b 11, seats > 40\\nc\\np seats\\nq\\n", "team 45"), "条件断点"),
        (pdb.format("c\\nup\\np sys.argv\\nq\\n", "enterprise 2"), "KeyError 后 up 查看 argv"),
        ("python -c \"import billing; print(billing.invoice('team', 50))\"", "50 座边界值"),
        ("python -c \"import billing; print(billing.invoice('team', 51))\"", "51 座触发 E-4127"),
        ("python -X faulthandler billing.py team 99", "faulthandler 运行"),
        (pdb.format("b 13\\nc\\ndisplay seats\\nn\\nq\\n", "team 52"), "display 变量"),
        ("python -c \"import billing,traceback\ntry: billing.invoice('team',60)\nexcept billing.BillingError as e: "
         "print('code=', e.code); traceback.print_exc()\"", "捕获异常打印 code"),
        ("grep -n 'E-4127' billing.py", "搜索错误码"),
        (pdb.format("b 10\\nc\\nbt\\np plan\\nq\\n", "basic 2"), "bt 与 plan 变量"),
        (pdb.format("b 16\\nc\\np {k: type(v).__name__ for k, v in locals().items()}\\nq\\n", "pro 4"), "局部变量类型"),
        ("python -m trace --count -C . billing.py pro 2 && head -20 billing.cover", "trace 覆盖计数"),
        (pdb.format("b 12\\nc\\np RATES\\nq\\n", "basic 1"), "全局 RATES"),
        ("python -m cProfile -s cumtime billing.py pro 5 | head -15", "cProfile"),
        (pdb.format("tbreak 15\\nc\\nargs\\nq\\n", "team 20 NUS15"), "临时断点 args"),
    ]
    sizes = [M, S, W, W, W, W, W, W, W, M, M, W, W, S, W, W, W, W, S, W, W, W, M, W, W]
    facts = {2: "cao-code-10#1", 4: "cao-code-10#2"}
    return [dict(cmd=c, size=sizes[i], keep=False, lang="en", var=v, facts=facts.get(i, ""))
            for i, (c, v) in enumerate(s)]


TASKS = {
    "cao-code-01": (setup_01, steps_01), "cao-code-02": (setup_02, steps_02),
    "cao-code-04": (setup_04, steps_04), "cao-code-05": (setup_05, steps_05),
    "cao-code-06": (setup_06, steps_06), "cao-code-07": (setup_07, steps_07),
    "cao-code-08": (setup_08, steps_08), "cao-code-09": (setup_09, steps_09),
    "cao-code-10": (setup_10, steps_10),
}

# 植入事实草稿（画面里有、以后可能被查询找回；查询真值仍由人确认）
FACTS = [
    ("cao-code-01#3", "cao-code-01", "error", "RuntimeError: shape '[32, 3, 192, 192]' is invalid for input of size 3840000",
     "en", "train.py --batch_size 32 --img 192 真实报错"),
    ("cao-code-02#1", "cao-code-02", "error", "flask==3.0.0 depends on werkzeug>=3.0.0; requirements unsatisfiable",
     "en", "uv 解析冲突"),
    ("cao-code-02#2", "cao-code-02", "error", "pandas==2.2.2 与 numpy<1.22 无解", "en", "uv 解析冲突"),
    ("cao-code-04#1", "cao-code-04", "assert", "test_dict_diff: {'qty': 3} != {'qty': 4}", "en", "pytest 字典 diff"),
    ("cao-code-04#2", "cao-code-04", "testid", "test_apply_discount[19.99-15-16.99]", "en", "参数化用例编号"),
    ("cao-code-05#1", "cao-code-05", "error", "bad_indent.yaml 第 3 行 mapping values are not allowed here", "en", "YAML 缩进"),
    ("cao-code-05#2", "cao-code-05", "config", "model_name: vit-small-patch16-224, batch_size 32", "en", "基础配置"),
    ("cao-code-06#1", "cao-code-06", "git", "CONFLICT (content): Merge conflict in params.yaml (feature/augment-mixup)",
     "en", "merge 冲突"),
    ("cao-code-06#2", "cao-code-06", "git", "! [rejected] main -> main (fetch first)", "en", "push rejected"),
    ("cao-code-07#1", "cao-code-07", "http", "HTTP/2 429 TOO MANY REQUESTS（httpbin）", "en", "状态码"),
    ("cao-code-07#2", "cao-code-07", "http", "httpbin /bearer 无 token 返回 401", "en", "错误响应"),
    ("cao-code-08#1", "cao-code-08", "error", "UNIQUE constraint failed: customers.email", "en", "sqlite 约束"),
    ("cao-code-08#2", "cao-code-08", "data", "订单 SO-10234 金额 999.00（Zurich 客户）", "en", "box 模式查询结果"),
    ("cao-code-09#1", "cao-code-09", "error", "undeclared.c:5:20: error: use of undeclared identifier 'totl'", "en",
     "clang 报错"),
    ("cao-code-09#2", "cao-code-09", "error", "segv 运行 Segmentation fault（exit=139）", "en", "运行时段错误"),
    ("cao-code-10#1", "cao-code-10", "error", "BillingError: [E-4127] seat limit exceeded: 64 > 50 for plan team", "en",
     "自定义错误码"),
    ("cao-code-10#2", "cao-code-10", "var", "pdb 断点处 rate=29.0 seats=3 subtotal=87.0", "en", "调试变量"),
]
