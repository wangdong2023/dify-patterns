#!/usr/bin/env python3
import os
import yaml
import requests
import typer
from pathlib import Path
import re
import unicodedata

app = typer.Typer(help="DFaC: Dify Flow as Code CLI (Console API Version)")

# Windows 保留名称（大小写不敏感）
WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1,10)),
    *(f"LPT{i}" for i in range(1,10)),
}

APPS_MAP_FILE = Path("dfac_apps.yaml")

def load_apps_map():
    if APPS_MAP_FILE.exists():
        return yaml.safe_load(APPS_MAP_FILE.read_text("utf-8"))
    return {"apps": []}

def save_apps_map(data):
    APPS_MAP_FILE.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")

def resolve_app_identifier(name_or_dir: str, apps_map: dict):
    # 用户传 ID
    if re.fullmatch(r"[0-9a-fA-F-]{36}", name_or_dir):
        return name_or_dir

    # 用户传目录
    for entry in apps_map["apps"]:
        if entry["dir"] == name_or_dir:
            return entry["id"]

    # 用户传 app name
    for entry in apps_map["apps"]:
        if entry["name"] == name_or_dir:
            return entry["id"]

    raise ValueError(f"找不到 app：{name_or_dir}")

def allocate_dir_for_app(app_name: str, app_id: str, apps_map: dict):
    base = ensure_filename(app_name)
    dir_name = base

    exists = {entry["dir"] for entry in apps_map["apps"]}

    suffix = 2
    while dir_name in exists:
        dir_name = f"{base}_{suffix}"
        suffix += 1

    apps_map["apps"].append({
        "name": app_name,
        "id": app_id,
        "dir": dir_name
    })
    save_apps_map(apps_map)
    return dir_name

def ensure_filename(name: str, extension: str | None = None) -> str:
    """
    Sanitize name for use as filename across Windows/macOS/Linux.
    Converts name into a safe filesystem-friendly filename.

    Args:
        name (str): Input name
        extension (str, optional): File extension like '.yaml'
    """

    # 1. Unicode Normalize（兼容各种字符，包括中文）
    name = unicodedata.normalize("NFKC", name)

    # 2. 替换非法字符（跨平台）
    # Windows 禁止: \ / : * ? " < > |
    # Linux 禁止: /
    # macOS 禁止: :
    invalid_chars = r'[\\/:*?"<>|\x00-\x1F]'
    name = re.sub(invalid_chars, "_", name)

    # 3. 去除前后空格与点（Windows 不允许以点或空格结尾）
    name = name.strip(" .")

    # 4. 空字符串则使用默认
    if not name:
        name = "unnamed"

    # 5. Windows 保留关键字处理（大小写不敏感）
    upper_name = name.upper()
    if upper_name in WINDOWS_RESERVED_NAMES:
        name = f"{name}_"

    # 6. 合并重复下划线
    name = re.sub(r"_+", "_", name)

    # 7. 如果有扩展名则添加
    if extension:
        extension = extension if extension.startswith(".") else "." + extension
        name = name + extension

    return name


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
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
    }
    resp = requests.post(url, json={"email": email, "password": password}, headers=headers)

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
        node_name = node["data"]["title"]
        node_name_path = ensure_filename(node_name)
        node_file = nodes_dir / f"{node_name_path}.yaml"
        node = dict(node)
        node_data = dict(node)["data"]  # deep copy

        # --- prompt 拆分 ---
        if "prompt_template" in node_data:
            prompts = []
            for prompt in node_data["prompt_template"]: 
                prompt_name = f"{node_name_path}__" + ensure_filename(prompt["role"])
                prompt_path = prompts_dir / f"{prompt_name}.md"
                prompt_path.write_text(prompt["text"], encoding="utf-8")
                prompt["text"] = {"ref": f"{prompt_path}"}
                prompts.append(prompt)
            node_data["prompt_template"] = prompts

        # --- script 拆分 ---
        if "code" in node_data and isinstance(node_data["code"], str):
            if "python" in node_data["code_language"]:
                script_path = code_dir / f"{node_name_path}.py"
            elif "javascript" in node_data["code_language"]:
                script_path = code_dir / f"{node_name_path}.js"
            else:
                script_path = code_dir / f"{node_name_path}.txt"
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
def build(flow_path: str = typer.Argument(help="flow path")):
    # cfg = load_config(Path(config) if config else None)
    flow_json = build_flow_from_files(Path(flow_path))
    typer.echo(yaml.safe_dump(flow_json, allow_unicode=True))


# ---------------------------------------------------------
# dfac pull (Dify → 本地)
# ---------------------------------------------------------
@app.command()
def pull(config: str = typer.Option(None, help="config 文件路径"), 
         app: str = typer.Argument(help="app id")):
    cfg = load_config(Path(config) if config else None)
    base = cfg["dify_base_url"]
    cookies = console_login(base, cfg["console_email"], cfg["console_password"])
    headers = {'X-Csrf-Token': cookies['csrf_token']}
    # 获取 DSL 定义
    url = f"{base}/console/api/apps/{app}/export?include_secret=false"
    resp = requests.get(url, cookies=cookies, headers=headers)

    if resp.status_code != 200:
        typer.secho(f"❌ 获取 app 失败: {resp.status_code} {resp.text}", fg="red")
        raise typer.Exit(code=1)

    dsl = resp.json()['data']
    dsl = yaml.safe_load(dsl)

    app_name = dsl["app"]["name"]
    apps_map = load_apps_map()
    dir_name = allocate_dir_for_app(app_name, app, apps_map)

    # 写入本地 main.yaml
    flow_dir = Path(cfg["flow_dir"]) / dir_name
    flow_dir.mkdir(parents=True, exist_ok=True)

    split_flow_to_files(dsl, flow_dir)

    typer.secho(f"✔ 已写入 {flow_dir}", fg="green")


# ---------------------------------------------------------
# dfac push (本地 → Dify)
# ---------------------------------------------------------
@app.command()
def push(config: str = typer.Option(None, help="config 文件路径"), 
         create_new: bool = typer.Option(False, help="create new app"),
         app: str = typer.Argument(help="app id")):
    cfg = load_config(Path(config) if config else None)
    apps_map = load_apps_map()

    # Step 1：根据用户输入（名称 / 目录 / id）找到实际 app_id
    app_id = resolve_app_identifier(app, apps_map)
    dir_name = next(entry["dir"] for entry in apps_map["apps"] if entry["id"] == app_id)

    typer.secho(f"✔ 推送 app {dir_name}, id {app_id}", fg="green")

    flow_dir = Path(cfg["flow_dir"]) / dir_name
    dsl = build_flow_from_files(flow_dir)

    base = cfg["dify_base_url"]
    cookies = console_login(base, cfg["console_email"], cfg["console_password"])

    headers = {
        'X-Csrf-Token': cookies['csrf_token'],
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
    }
    payload = {
        "mode": "yaml-content",
        "yaml_content": yaml.safe_dump(dsl, allow_unicode=True)
    }

    if not create_new:
        payload["app_id"] = app_id
    url = f"{base}/console/api/apps/imports"
    resp = requests.post(url, headers=headers, cookies=cookies, json=payload)

    if resp.status_code not in [200, 201]:
        print(resp.json())
        typer.secho(f"❌ 推送失败: {resp.status_code} {resp.text}", fg="red")
        raise typer.Exit(code=1)

    typer.secho("✔ app 推送成功！", fg="green")


if __name__ == "__main__":
    app()
