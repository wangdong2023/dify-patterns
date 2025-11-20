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
    print(resp.cookies.get_dict())
    return resp.cookies.get_dict()


# ---------------------------------------------------------
# 构建 DSL（本地 → JSON）
# ---------------------------------------------------------
def load_yaml(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def build_flow(flow_dir: Path):
    main_path = flow_dir / "main.yaml"
    main = load_yaml(main_path)

    nodes = []
    for n in main.get("graph", {}).get("nodes", []):
        ref = n.get("ref")
        if not ref:
            continue

        node_path = (flow_dir / ref).resolve()
        node = load_yaml(node_path)

        # prompt
        if isinstance(node.get("prompt"), str):
            p = (flow_dir / node["prompt"]).resolve()
            if p.exists():
                node["prompt"] = p.read_text(encoding="utf-8")

        # script
        if isinstance(node.get("script"), str):
            s = (flow_dir / node["script"]).resolve()
            if s.exists():
                node["script"] = s.read_text(encoding="utf-8")

        nodes.append(node)

    main["graph"]["nodes"] = nodes
    return main


# ---------------------------------------------------------
# dfac build
# ---------------------------------------------------------
@app.command()
def build(config: str = typer.Option(None, help="指定 config 文件路径")):
    cfg = load_config(Path(config) if config else None)
    flow_json = build_flow(Path(cfg["flow_dir"]))
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
    print(cookies)
    resp = requests.get(url, cookies=cookies, headers=headers)

    if resp.status_code != 200:
        typer.secho(f"❌ 获取 Workflow 失败: {resp.status_code} {resp.text}", fg="red")
        raise typer.Exit(code=1)

    dsl = resp.json()['data']

    # 写入本地 main.yaml
    flow_dir = Path(cfg["flow_dir"])
    flow_dir.mkdir(parents=True, exist_ok=True)

    (flow_dir / "main.yaml").write_text(
        yaml.safe_dump(dsl, allow_unicode=True), encoding="utf-8"
    )

    typer.secho("✔ 已写入 flow/main.yaml", fg="green")


# ---------------------------------------------------------
# dfac push (本地 → Dify)
# ---------------------------------------------------------
@app.command()
def push(config: str = typer.Option(None, help="config 文件路径")):
    cfg = load_config(Path(config) if config else None)

    base = cfg["dify_base_url"]
    app_id = cfg["app_id"]
    workflow_id = cfg["workflow_id"]
    token = console_login(base, cfg["console_email"], cfg["console_password"])

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    flow_dir = Path(cfg["flow_dir"])
    dsl = build_flow(flow_dir)

    url = f"{base}/console/api/apps/{app_id}/workflows/{workflow_id}/definition"
    resp = requests.put(url, headers=headers, json=dsl)

    if resp.status_code not in [200, 201]:
        typer.secho(f"❌ 推送失败: {resp.status_code} {resp.text}", fg="red")
        raise typer.Exit(code=1)

    typer.secho("✔ Workflow 推送成功！", fg="green")


if __name__ == "__main__":
    app()
