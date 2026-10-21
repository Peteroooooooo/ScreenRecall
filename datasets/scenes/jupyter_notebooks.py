"""code-03：生成 5 个受控 notebook 并用真实 Python 内核执行（allow_errors，报错保留为真实 traceback）。

用法：data-dataset/.tools/jupyter-venv/bin/python datasets/scenes/jupyter_notebooks.py <输出目录>
数据全部虚构；输出目录名不含任务编号（避免 OCR 读到类别线索）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import nbformat
from nbclient import NotebookClient
from nbformat.v4 import new_code_cell, new_markdown_cell, new_notebook

SETUP = "import pandas as pd\nimport numpy as np\npd.set_option('display.width', 120)\n"

NBS = {
    "orders_keyerror.ipynb": [
        ("md", "# Order export check (synthetic data)"),
        SETUP + "orders = pd.DataFrame({\n    'order_id': ['ORD-48213', 'ORD-48214', 'ORD-48215', 'ORD-48216', 'ORD-48217'],\n"
                "    'customer': ['Lim', 'Tan', 'Ng', 'Wong', 'Lee'],\n    'amount_sgd': [129.90, 45.00, 310.50, 18.75, 76.20],\n"
                "    'status': ['paid', 'refunded', 'paid', 'pending', 'paid']})\norders",
        "orders.describe()",
        "# KeyError: the export renamed the column\norders['total_amount'].sum()",
        "orders.columns.tolist()",
        "paid = orders[orders['status'] == 'paid']\npaid['amount_sgd'].sum()",
        "orders.groupby('status')['amount_sgd'].agg(['count', 'sum'])",
        "orders.loc[orders['amount_sgd'] > 100, ['order_id', 'customer', 'amount_sgd']]",
    ],
    "sensor_valueerror.ipynb": [
        ("md", "# Lab sensor readings (synthetic)"),
        SETUP + "raw = pd.DataFrame({'ts': pd.date_range('2026-10-01 09:00', periods=8, freq='15min'),\n"
                "    'temp_c': ['21.4', '21.9', '22.3', 'n/a', '23.0', '23.6', '24.1', '24.4'],\n"
                "    'room': ['E4-04-02'] * 8})\nraw",
        "# ValueError: 'n/a' cannot be converted\nraw['temp_c'].astype(float)",
        "raw['temp_c'] = pd.to_numeric(raw['temp_c'], errors='coerce')\nraw.isna().sum()",
        "raw['temp_c'] = raw['temp_c'].interpolate()\nraw",
        "raw.set_index('ts')['temp_c'].resample('30min').mean()",
        "np.round(raw['temp_c'].diff().describe(), 3)",
    ],
    "merge_mismatch.ipynb": [
        ("md", "# Join students to grades (synthetic)"),
        SETUP + "students = pd.DataFrame({'matric': ['A0251001X', 'A0251002Y', 'A0251003Z', 'A0251004W'],\n"
                "    'name': ['Alice', 'Bob', 'Chen', 'Dina']})\nstudents",
        "grades = pd.DataFrame({'matric_no': ['A0251001X', 'A0251003Z', 'A0251005V'],\n    'quiz1': [8.5, 9.0, 6.5], 'quiz2': [7.0, 9.5, 8.0]})\ngrades",
        "# MergeError: no common columns\npd.merge(students, grades)",
        "merged = students.merge(grades, left_on='matric', right_on='matric_no', how='left', indicator=True)\nmerged",
        "merged['_merge'].value_counts()",
        "merged.assign(avg=merged[['quiz1', 'quiz2']].mean(axis=1)).drop(columns='matric_no')",
    ],
    "dtype_parse.ipynb": [
        ("md", "# Parse invoice dates (synthetic)"),
        SETUP + "inv = pd.DataFrame({'invoice': ['INV-2026-0911', 'INV-2026-0912', 'INV-2026-0913', 'INV-2026-0914'],\n"
                "    'issued': ['2026-09-11', '11/09/2026', '2026-09-13', '2026-09-14'],\n    'qty': [3, 1, 12, 5], 'unit_price': [19.9, 249.0, 2.5, 38.0]})\ninv",
        "# ValueError: mixed date formats\npd.to_datetime(inv['issued'], format='%Y-%m-%d')",
        "inv['issued'] = pd.to_datetime(inv['issued'], format='mixed', dayfirst=True)\ninv.dtypes",
        "inv['line_total'] = inv['qty'] * inv['unit_price']\ninv",
        "# TypeError: str and int\ninv['invoice'] + 1",
        "inv.sort_values('line_total', ascending=False).head(3)",
    ],
    "groupby_report.ipynb": [
        ("md", "# Weekly study-hours report (synthetic)"),
        SETUP + "rng = np.random.default_rng(42)\nlog = pd.DataFrame({'week': np.repeat([41, 42, 43], 6),\n"
                "    'member': ['Cao', 'Xiao', 'Rao'] * 6, 'module': ['PM2', 'ML'] * 9,\n    'hours': rng.integers(1, 9, 18)})\nlog.head(10)",
        "pivot = log.pivot_table(index='member', columns='week', values='hours', aggfunc='sum')\npivot",
        "# KeyError: week 44 not logged yet\npivot[44]",
        "pivot.loc[:, 41:43].sum(axis=1).rename('total_hours')",
        "log.groupby(['module', 'member'])['hours'].mean().unstack()",
        "# AttributeError: typo in method name\nlog.groupby('member').hours.summ()",
    ],
}


def build(out: Path) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, cells in NBS.items():
        nb = new_notebook(cells=[new_markdown_cell(c[1]) if isinstance(c, tuple) else new_code_cell(c) for c in cells])
        nb.metadata["kernelspec"] = {"name": "python3", "display_name": "Python 3", "language": "python"}
        NotebookClient(nb, timeout=120, allow_errors=True, kernel_name="python3").execute()
        p = out / name
        nbformat.write(nb, p)
        errs = sum(1 for c in nb.cells if c.cell_type == "code" for o in c.get("outputs", []) if o.output_type == "error")
        print(f"{name}: {len(nb.cells)} 个单元格，真实报错 {errs} 个")
        paths.append(p)
    return paths


if __name__ == "__main__":
    build(Path(sys.argv[1]))
