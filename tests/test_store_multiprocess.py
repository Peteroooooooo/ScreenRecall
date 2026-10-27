"""v2 存储的跨进程行为：另一个进程写库 / 追加向量时本进程增量读；读者占着旧向量文件时压缩照常完成。"""

import subprocess
import sys
import textwrap
from pathlib import Path

import numpy as np

from screenrecall.store import schema
from screenrecall.store.fts import lexical
from screenrecall.store.vectors import VectorStore

ROOT = Path(__file__).resolve().parent.parent
DIM = 8


def run_child(code: str, *args: str, timeout: float = 60) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", textwrap.dedent(code), *args], cwd=str(ROOT),
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def test_reader_sees_rows_written_by_other_process(tmp_path):
    child = run_child("""
        import sys
        from screenrecall.store.writer import Writer, LineIn
        from screenrecall.store.vectors import VectorStore
        import numpy as np
        w = Writer(sys.argv[1])
        vs = VectorStore(sys.argv[1] + '/vectors/text', 8)
        sid = w.segment_for('child.exe', '子进程', 1000).result(10)
        b = w.block_new(sid, 1000).result(10)
        ids = w.record_lines(b, 1000, (0, 100), [LineIn('子进程写入的行', 0, 10, 100, 30)]).result(10)
        vs.append(ids, np.eye(8, dtype=np.float32)[:1], [1000], [sid])
        print('ready', flush=True)
        sys.stdin.readline()
        ids = w.record_lines(b, 2000, (0, 100), [LineIn('子进程写入的行', 0, 10, 100, 30),
                                                LineIn('第二批', 0, 40, 100, 60)]).result(10)
        vs.append(ids[1:], np.eye(8, dtype=np.float32)[1:2], [2000], [sid])
        w.close()
        print('done', flush=True)
    """, str(tmp_path))
    assert child.stdout.readline().strip() == "ready"
    c = schema.connect_ro(schema.db_path(tmp_path))
    vs = VectorStore(tmp_path / "vectors" / "text", DIM, readonly=True)
    assert lexical.search(c, "子进程").total == 1 and len(vs) == 1
    child.stdin.write("go\n")
    child.stdin.flush()
    assert child.stdout.readline().strip() == "done", child.stderr.read()
    child.wait(30)
    assert lexical.search(c, "第二批").total == 1
    vs.refresh()
    assert len(vs) == 2 and vs.search(np.eye(DIM)[1], 1)[0].ts == 2000
    c.close()


def test_compact_while_other_process_maps_old_files(tmp_path):
    root = tmp_path / "v"
    w = VectorStore(root, DIM, segment_rows=50)
    rng = np.random.default_rng(0)
    vecs = rng.standard_normal((120, DIM)).astype(np.float32)
    w.append(np.arange(1, 121), vecs, np.arange(120), np.arange(120) % 5)
    child = run_child("""
        import sys
        import numpy as np
        from screenrecall.store.vectors import VectorStore
        r = VectorStore(sys.argv[1], 8, readonly=True)
        print(len(r), flush=True)
        sys.stdin.readline()          # 父进程压缩期间一直占着旧文件的映射
        r.refresh()
        print(len(r), r.search(np.ones(8), 3)[0].id > 0, flush=True)
    """, str(root))
    assert child.stdout.readline().strip() == "120"
    w.delete(np.arange(1, 31))
    assert w.compact() == 1  # 第 0 段全是死行之外的部分；最后一段不动
    child.stdin.write("go\n")
    child.stdin.flush()
    out = child.stdout.readline().split()
    child.wait(30)
    assert out == ["90", "True"], child.stderr.read()
    # 子进程退出后，下一次打开写入方会把旧一代文件删掉
    VectorStore(root, DIM)
    assert not list(root.glob("s00000.g0.*"))
