#!/usr/bin/env python3
import os
import yaml
import requests
import typer
from pathlib import Path

app = typer.Typer(help="DFaC: Dify Flow as Code CLI (Console API Version)")

# ---------------------------------------------------------
# 加载 dfac.yaml
# ---------------------------------------------------------
def load_config(config_path: Path = None):
    cfg = {}

    # 显式指定
    if config_path and config_path.exists():
        cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    # 默认 dfac.yaml
    elif Path("dfac.yaml").exists():
        cfg = yaml.safe_load(Path("dfac.yaml").read_text(encoding="utf-8"))

    # 最低字段
    cfg.setdefault("flow_dir", "./flow")
    cfg.setdefault("dify_base_url", os.getenv("DIFY_BASE_URL", "http://localhost:5001"))

    # Console API 登录信息
    cfg.setdefault("console_email", os.getenv("DIFY_CONSOLE_EMAIL"))
    cfg.setdefault("console_password", os.getenv("DIFY_CONSOLE_PASSWORD"))

    if not cfg.get("console_email") or not cfg.get("console_password"):
        typer.secho("❌ 需要 console_email 与 console_password", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    return cfg


# ---------------------------------------------------------
# Console API 登录
# ---------------------------------------------------------
def console_login(base_url, email, password):
    url = f"{base_url}/console/api/login"
    resp = requests.post(url, json={"email": email, "password": password})

    if resp.status_code != 200:
        typer.secho(f"❌ 登录失败: {resp.status_code} {resp.text}", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    return resp.cookies.get_dict()


# ---------------------------------------------------------
# 构建 DSL（本地 → JSON）
# ---------------------------------------------------------
def load_yaml(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def split_flow_to_files(dsl: dict, flow_dir: Path):
    nodes_dir = flow_dir / "nodes"
    prompts_dir = flow_dir / "prompts"
    code_dir = flow_dir / "code"

    nodes_dir.mkdir(exist_ok=True, parents=True)
    prompts_dir.mkdir(exist_ok=True)
    code_dir.mkdir(exist_ok=True)

    nodes = []

    for node in dsl["workflow"]["graph"]["nodes"]:
        node_id = node["id"]
        node_file = nodes_dir / f"{node_id}.yaml"
        node = dict(node)
        node_data = dict(node)["data"]  # deep copy

        # --- prompt 拆分 ---
        if "prompt_template" in node_data:
            prompts = []
            for prompt in node_data["prompt_template"]: 
                prompt_id = prompt["id"]
                prompt_path = prompts_dir / f"{prompt_id}.md"
                prompt_path.write_text(prompt["text"], encoding="utf-8")
                prompt["text"] = {"ref": f"{prompt_path}"}
                prompts.append(prompt)
            node_data["prompt_template"] = prompts

        # --- script 拆分 ---
        if "code" in node_data and isinstance(node_data["code"], str):
            if "python" in node_data["code_language"]:
                script_path = code_dir / f"{node_id}.py"
            elif "javascript" in node_data["code_language"]:
                script_path = code_dir / f"{node_id}.js"
            else:
                script_path = code_dir / f"{node_id}.txt"
            script_path.write_text(node_data["code"], encoding="utf-8")
            node_data["code"] = {"ref": f"{script_path}"}
        node["data"] = node_data
        # 写入 node yaml
        with node_file.open("w", encoding="utf-8") as f:
            yaml.safe_dump(node, f, allow_unicode=True)

        # main.yaml 的 nodes 用引用写法
        nodes.append({
            "ref": f"{node_file}"
        })
    dsl["workflow"]["graph"]["nodes"] = nodes
    # 写入 main.yaml
    with (flow_dir / "main.yaml").open("w", encoding="utf-8") as f:
        yaml.safe_dump(dsl, f, allow_unicode=True)


def build_flow_from_files(flow_dir: Path):
    # 读取 main.yaml
    main = load_yaml(flow_dir / "main.yaml")

    # 最终结果 DSL
    dsl = dict(main)  # deep copy
    nodes = []

    for n in main["workflow"]["graph"]["nodes"]:
        ref = n["ref"]
        node_path = Path(ref).resolve()
        # 加载 node yaml
        node = load_yaml(node_path)
        node_data = node["data"]

        # ----------- 恢复 prompt_template 文本内容 -----------
        if "prompt_template" in node_data:
            prompts = []
            for prompt in node_data["prompt_template"]:
                text_ref = prompt["text"]
                if isinstance(text_ref, dict) and "ref" in text_ref:
                    prompt_path = Path(text_ref["ref"]).resolve()
                    prompt["text"] = prompt_path.read_text(encoding="utf-8")
                prompts.append(prompt)

            node_data["prompt_template"] = prompts

        # ----------- 恢复 code 文本内容 -----------
        if "code" in node_data:
            code_ref = node_data["code"]
            if isinstance(code_ref, dict) and "ref" in code_ref:
                code_path = Path(code_ref["ref"]).resolve()
                node_data["code"] = code_path.read_text(encoding="utf-8")

        # 写回 node 结构
        node["data"] = node_data

        # 添加到新的 DSL nodes[]
        nodes.append(node)
    dsl["workflow"]["graph"]["nodes"] = nodes
    return dsl



# ---------------------------------------------------------
# dfac build
# ---------------------------------------------------------
@app.command()
def build(config: str = typer.Option(None, help="指定 config 文件路径")):
    cfg = load_config(Path(config) if config else None)
    flow_json = build_flow_from_files(Path(cfg["flow_dir"]))
    typer.echo(yaml.safe_dump(flow_json, allow_unicode=True))


# ---------------------------------------------------------
# dfac pull (Dify → 本地)
# ---------------------------------------------------------
@app.command()
def pull(config: str = typer.Option(None, help="config 文件路径")):
    cfg = load_config(Path(config) if config else None)

    base = cfg["dify_base_url"]

    workflow_id = cfg["workflow_id"]
    cookies = console_login(base, cfg["console_email"], cfg["console_password"])
    headers = {'X-Csrf-Token': cookies['csrf_token']}
    # 获取 DSL 定义
    url = f"{base}/console/api/apps/{workflow_id}/export?include_secret=false"
    resp = requests.get(url, cookies=cookies, headers=headers)

    if resp.status_code != 200:
        typer.secho(f"❌ 获取 Workflow 失败: {resp.status_code} {resp.text}", fg="red")
        raise typer.Exit(code=1)

    dsl = resp.json()['data']
    dsl = yaml.safe_load(dsl)

    # 写入本地 main.yaml
    flow_dir = Path(cfg["flow_dir"])
    flow_dir.mkdir(parents=True, exist_ok=True)

    split_flow_to_files(dsl, flow_dir)

    typer.secho("✔ 已写入 flow/main.yaml", fg="green")


# ---------------------------------------------------------
# dfac push (本地 → Dify)
# ---------------------------------------------------------
@app.command()
def push(config: str = typer.Option(None, help="config 文件路径"), create_new: str = typer.Option(False, help="create new app")):
    cfg = load_config(Path(config) if config else None)

    flow_dir = Path(cfg["flow_dir"])
    dsl = build_flow_from_files(flow_dir)

    # print(dsl)

    base = cfg["dify_base_url"]
    workflow_id = cfg["workflow_id"]
    cookies = console_login(base, cfg["console_email"], cfg["console_password"])
    print(cookies)
    headers = {'X-Csrf-Token': cookies['csrf_token']}
    payload = {
        "mode": "yaml-content",
        "yaml-content": dsl
    }
    if not create_new:
        payload["app_id"] = workflow_id
    url = f"{base}/console/api/apps/imports"
    resp = requests.post(url, headers=headers, json=payload)

    if resp.status_code not in [200, 201]:
        typer.secho(f"❌ 推送失败: {resp.status_code} {resp.text}", fg="red")
        raise typer.Exit(code=1)

    # typer.secho("✔ Workflow 推送成功！", fg="green")


if __name__ == "__main__":
    app()
