#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Deploy automatizado do portal POPULIS na AWS:

- Empacota o backend (venv local ou Docker para ZIP compatível com Linux/Lambda)
- Cria ou reutiliza role IAM para Lambda
- Cria ou atualiza a função Lambda + Function URL (CORS)
- Cria bucket S3 automaticamente (nome gerado ou informado) e envia o frontend

Uso (na pasta ``portal``):

    pip install -r requirements-deploy.txt
    python deploy_aws.py --region us-east-1 --public-website

Para ZIP compatível com Lambda na AWS (Linux), use ``--use-docker`` (requer Docker).

Credenciais: perfil/credenciais padrão do boto3 (``AWS_PROFILE``, ``AWS_ACCESS_KEY_ID``, etc.).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any

try:
    import boto3
    from botocore.exceptions import ClientError
except ImportError as exc:  # pragma: no cover
    print("Instale boto3: pip install -r requirements-deploy.txt", file=sys.stderr)
    raise SystemExit(1) from exc

LOG = logging.getLogger("deploy_aws")

LAMBDA_TRUST_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {"Service": "lambda.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }
    ],
}

CORS_CONFIG = {
    "AllowCredentials": False,
    "AllowHeaders": ["content-type", "authorization"],
    "AllowMethods": ["*"],
    "AllowOrigins": ["*"],
    "ExposeHeaders": [],
    "MaxAge": 86400,
}


def project_paths(script_path: Path) -> tuple[Path, Path, Path]:
    """Retorna (portal_dir, backend_dir, frontend_dir)."""
    portal = script_path.resolve().parent
    if portal.name != "portal":
        candidate = portal / "portal"
        if candidate.is_dir():
            portal = candidate
    return portal, portal / "backend", portal / "frontend"


def discover_python() -> str:
    return sys.executable


def build_lambda_zip_linux_docker(backend_dir: Path, log: logging.Logger) -> Path:
    """Gera ``function.zip`` via container Linux (wheels compatíveis com Lambda)."""
    zip_out = backend_dir / "function.zip"
    mount = str(backend_dir.resolve()).replace("\\", "/")
    inner = (
        "set -e && apt-get update -qq && apt-get install -y -qq zip >/dev/null "
        "&& rm -rf package function.zip && mkdir -p package "
        "&& pip install --no-cache-dir -r requirements.txt -t package "
        "&& cp handler.py package/ && cd package && zip -r ../function.zip ."
    )
    cmd = [
        "docker",
        "run",
        "--rm",
        "-v",
        f"{mount}:/work",
        "-w",
        "/work",
        "python:3.12-slim",
        "bash",
        "-lc",
        inner,
    ]
    log.info("Empacotando Lambda com Docker (python:3.12-slim)...")
    subprocess.run(cmd, check=True)
    if not zip_out.is_file():
        raise FileNotFoundError(f"ZIP não gerado: {zip_out}")
    return zip_out


def build_lambda_zip_venv(backend_dir: Path, log: logging.Logger) -> Path:
    """Gera ``function.zip`` com venv local."""
    pkg = backend_dir / "package"
    venv_dir = backend_dir / "_venv_deploy_pack"
    zip_out = backend_dir / "function.zip"
    if pkg.exists():
        shutil.rmtree(pkg)
    if venv_dir.exists():
        shutil.rmtree(venv_dir)
    if zip_out.exists():
        zip_out.unlink()

    py = discover_python()
    log.info("Criando venv temporário em %s", venv_dir)
    subprocess.run([py, "-m", "venv", str(venv_dir)], check=True, cwd=str(backend_dir))

    if os.name == "nt":
        pip_exe = venv_dir / "Scripts" / "pip.exe"
    else:
        pip_exe = venv_dir / "bin" / "pip"

    pkg.mkdir(parents=True)
    log.info("Instalando dependências em package/ ...")
    subprocess.run(
        [str(pip_exe), "install", "--disable-pip-version-check", "-r", "requirements.txt", "-t", "package"],
        check=True,
        cwd=str(backend_dir),
    )

    shutil.copy2(backend_dir / "handler.py", pkg / "handler.py")

    log.info("Gerando function.zip")
    with zipfile.ZipFile(zip_out, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _, files in os.walk(pkg):
            for name in files:
                fp = Path(root) / name
                arc = fp.relative_to(pkg).as_posix()
                zf.write(fp, arcname=arc)

    shutil.rmtree(venv_dir, ignore_errors=True)
    return zip_out


def account_id(sts) -> str:
    return sts.get_caller_identity()["Account"]


def ensure_iam_role(iam, role_name: str, log: logging.Logger) -> str:
    try:
        resp = iam.get_role(RoleName=role_name)
        log.info("Role IAM já existe: %s", role_name)
        return resp["Role"]["Arn"]
    except ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchEntity":
            raise
    log.info("Criando role IAM %s", role_name)
    iam.create_role(
        RoleName=role_name,
        AssumeRolePolicyDocument=json.dumps(LAMBDA_TRUST_POLICY),
        Description="Execução Lambda portal POPULIS",
    )
    iam.attach_role_policy(
        RoleName=role_name,
        PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
    )
    time.sleep(10)
    return iam.get_role(RoleName=role_name)["Role"]["Arn"]


def wait_lambda_ready(
    lam,
    function_name: str,
    log: logging.Logger,
    timeout_sec: int = 300,
) -> None:
    """Aguarda ``LastUpdateStatus`` sair de ``InProgress`` após ``update_function_code`` etc."""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        cfg = lam.get_function_configuration(FunctionName=function_name)
        state = cfg.get("State", "Active")
        upd = cfg.get("LastUpdateStatus") or "Successful"
        if state == "Failed":
            raise RuntimeError(cfg.get("StateReason", "Lambda em estado Failed"))
        if upd == "Failed":
            raise RuntimeError(cfg.get("LastUpdateStatusReason", "Atualização da Lambda falhou"))
        if upd == "InProgress":
            log.info("Aguardando Lambda concluir atualização...")
            time.sleep(4)
            continue
        return
    raise TimeoutError("Timeout aguardando Lambda após atualização do código.")


def create_or_update_lambda(
    lam,
    s3_client,
    function_name: str,
    role_arn: str,
    zip_path: Path,
    region: str,
    runtime: str,
    log: logging.Logger,
    staging_bucket: str | None,
    staging_prefix: str,
) -> None:
    del region  # reservado para futuros usos region-specific
    zip_bytes = zip_path.read_bytes()
    size_mb = len(zip_bytes) / (1024 * 1024)
    code: dict[str, Any]
    if size_mb <= 48:
        code = {"ZipFile": zip_bytes}
        log.info("Enviando código Lambda direto (%.2f MB)", size_mb)
    else:
        if not staging_bucket:
            raise ValueError(
                "ZIP > 48 MB: use --staging-bucket ou Lambda Layers / imagem OCI."
            )
        key = f"{staging_prefix.strip('/')}/function-{uuid.uuid4().hex[:12]}.zip"
        log.info("ZIP grande (%.2f MB); enviando para s3://%s/%s", size_mb, staging_bucket, key)
        s3_client.put_object(Bucket=staging_bucket, Key=key, Body=zip_bytes)
        code = {"S3Bucket": staging_bucket, "S3Key": key}

    common = dict(
        Runtime=runtime,
        Role=role_arn,
        Handler="handler.lambda_handler",
        Timeout=120,
        MemorySize=512,
        Architectures=["x86_64"],
        Code=code,
    )

    try:
        lam.get_function(FunctionName=function_name)
        log.info("Atualizando código da função existente...")
        if "ZipFile" in code:
            lam.update_function_code(FunctionName=function_name, ZipFile=code["ZipFile"])
        else:
            lam.update_function_code(
                FunctionName=function_name,
                S3Bucket=code["S3Bucket"],
                S3Key=code["S3Key"],
            )
        wait_lambda_ready(lam, function_name, log)
        for attempt in range(12):
            try:
                lam.update_function_configuration(
                    FunctionName=function_name,
                    Timeout=120,
                    MemorySize=512,
                    Handler="handler.lambda_handler",
                    Runtime=runtime,
                )
                break
            except ClientError as ue:
                if (
                    ue.response["Error"]["Code"] == "ResourceConflictException"
                    and attempt < 11
                ):
                    log.warning("Lambda ocupada; nova tentativa em 5s (%s/12)...", attempt + 1)
                    time.sleep(5)
                    wait_lambda_ready(lam, function_name, log)
                    continue
                raise
        wait_lambda_ready(lam, function_name, log)
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
        log.info("Criando função Lambda %s", function_name)
        attempts = 0
        while True:
            attempts += 1
            try:
                lam.create_function(FunctionName=function_name, **common)
                break
            except ClientError as ce:
                err = ce.response["Error"].get("Code", "")
                if err == "InvalidParameterValueException" and attempts < 10:
                    log.warning("Role propagando; aguardando 5s...")
                    time.sleep(5)
                    continue
                raise


def ensure_public_function_url_permissions(lam, function_name: str, log: logging.Logger) -> None:
    """
    Function URL com AuthType NONE exige política na função permitindo invocação pública.
    A AWS pede ``lambda:InvokeFunctionUrl`` e ``lambda:InvokeFunction`` para Principal ``*``.
    Ver aviso no console em Configuration → Function URL.
    """
    specs: list[tuple[str, str, dict[str, Any]]] = [
        (
            "PopulisPortalInvokeFunctionUrl",
            "lambda:InvokeFunctionUrl",
            {"FunctionUrlAuthType": "NONE"},
        ),
        (
            "PopulisPortalInvokeFunction",
            "lambda:InvokeFunction",
            {},
        ),
    ]
    for statement_id, action, extras in specs:
        try:
            kw: dict[str, Any] = {
                "FunctionName": function_name,
                "StatementId": statement_id,
                "Action": action,
                "Principal": "*",
            }
            kw.update(extras)
            lam.add_permission(**kw)
            log.info("Política na função: %s", action)
        except ClientError as pe:
            code = pe.response["Error"]["Code"]
            if code == "ResourceConflictException":
                log.debug("Política já existente: %s", statement_id)
            else:
                log.warning("add_permission %s: %s", action, pe)


def ensure_function_url(lam, function_name: str, log: logging.Logger) -> str:
    try:
        cfg = lam.get_function_url_config(FunctionName=function_name)
        url = cfg["FunctionUrl"]
        log.info("Function URL já existe.")
        out = url.rstrip("/")
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
        log.info("Criando Function URL (AuthType NONE)...")
        resp = lam.create_function_url_config(
            FunctionName=function_name,
            AuthType="NONE",
            Cors=CORS_CONFIG,
        )
        out = resp["FunctionUrl"].rstrip("/")

    ensure_public_function_url_permissions(lam, function_name, log)
    return out


def patch_config_js(content: str, api_base: str) -> str:
    escaped = json.dumps(api_base)
    return re.sub(
        r"window\.PORTAL_API_BASE\s*=\s*[^;]+;",
        f"window.PORTAL_API_BASE = {escaped};",
        content,
        count=1,
    )


def guess_content_type(path: Path) -> str:
    import mimetypes

    mimetypes.add_type("application/javascript", ".js")
    ct, _ = mimetypes.guess_type(str(path))
    return ct or "application/octet-stream"


def create_bucket(s3_client, bucket_name: str, region: str, log: logging.Logger) -> None:
    try:
        s3_client.head_bucket(Bucket=bucket_name)
        log.info("Bucket já existe: %s", bucket_name)
        return
    except ClientError as e:
        code = e.response["Error"].get("Code", "")
        if code not in ("404", "403", "NoSuchBucket"):
            raise

    log.info("Criando bucket %s na região %s", bucket_name, region)
    params: dict[str, Any] = {"Bucket": bucket_name}
    if region != "us-east-1":
        params["CreateBucketConfiguration"] = {"LocationConstraint": region}
    s3_client.create_bucket(**params)


def configure_static_website(s3_client, bucket_name: str, log: logging.Logger) -> None:
    log.info("Configurando website estático público (GetObject)...")
    s3_client.put_public_access_block(
        Bucket=bucket_name,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": False,
            "IgnorePublicAcls": False,
            "BlockPublicPolicy": False,
            "RestrictPublicBuckets": False,
        },
    )
    s3_client.put_bucket_website(
        Bucket=bucket_name,
        WebsiteConfiguration={"IndexDocument": {"Suffix": "index.html"}},
    )
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "PublicReadGetObject",
                "Effect": "Allow",
                "Principal": "*",
                "Action": "s3:GetObject",
                "Resource": f"arn:aws:s3:::{bucket_name}/*",
            }
        ],
    }
    s3_client.put_bucket_policy(Bucket=bucket_name, Policy=json.dumps(policy))


def sync_frontend(
    s3_client,
    bucket_name: str,
    frontend_dir: Path,
    api_base_url: str,
    write_local_config: Path | None,
    log: logging.Logger,
) -> None:
    cfg_rel = Path("js/config.js")
    patched_txt: str | None = None
    cfg_file = frontend_dir / cfg_rel
    if cfg_file.is_file():
        patched_txt = patch_config_js(cfg_file.read_text(encoding="utf-8"), api_base_url)
        if write_local_config:
            write_local_config.parent.mkdir(parents=True, exist_ok=True)
            write_local_config.write_text(patched_txt, encoding="utf-8")
            log.info("Salvo config local: %s", write_local_config)

    count = 0
    for fp in frontend_dir.rglob("*"):
        if fp.is_dir():
            continue
        rel = fp.relative_to(frontend_dir).as_posix()
        if patched_txt is not None and rel == cfg_rel.as_posix():
            body = patched_txt.encode("utf-8")
        else:
            body = fp.read_bytes()
        ct = guess_content_type(fp)
        s3_client.put_object(Bucket=bucket_name, Key=rel, Body=body, ContentType=ct)
        count += 1
        log.debug("s3://%s/%s", bucket_name, rel)

    log.info("Upload do frontend: %s arquivo(s).", count)


def website_endpoint(bucket_name: str, region: str) -> str:
    if region == "us-east-1":
        host = f"{bucket_name}.s3-website-us-east-1.amazonaws.com"
    else:
        host = f"{bucket_name}.s3-website.{region}.amazonaws.com"
    return f"http://{host}/"


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Deploy portal POPULIS (S3 + Lambda + Function URL).")
    p.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    p.add_argument("--profile", default=os.environ.get("AWS_PROFILE"))
    p.add_argument("--bucket-name", default=None, help="Nome global do bucket; omitido = gerado automaticamente.")
    p.add_argument("--lambda-name", default="populis-portal-api")
    p.add_argument("--role-name", default="populis-portal-lambda-exec")
    p.add_argument("--runtime", default="python3.12")
    p.add_argument("--use-docker", action="store_true", help="Build Linux do ZIP via Docker (recomendado para Lambda).")
    p.add_argument("--skip-packaging", action="store_true", help="Usar backend/function.zip existente.")
    p.add_argument("--staging-bucket", default=None, help="Bucket para ZIP da Lambda se > 48 MB.")
    p.add_argument("--public-website", action="store_true", help="Website S3 público + política de leitura.")
    p.add_argument("--write-local-config", action="store_true", help="Gravar portal/frontend/js/config.js com a URL da API.")
    p.add_argument("--skip-s3", action="store_true")
    p.add_argument("--skip-lambda", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s")

    portal_dir, backend_dir, frontend_dir = project_paths(Path(__file__))
    if not backend_dir.is_dir() or not frontend_dir.is_dir():
        LOG.error("portal/backend ou portal/frontend não encontrados em %s", portal_dir)
        return 2

    session_kw: dict[str, Any] = {"region_name": args.region}
    if args.profile:
        session_kw["profile_name"] = args.profile
    session = boto3.Session(**session_kw)
    sts = session.client("sts")
    iam = session.client("iam")
    lam = session.client("lambda")
    s3_client = session.client("s3")

    aid = account_id(sts)
    bucket_name = args.bucket_name
    if not bucket_name and not args.skip_s3:
        suffix = uuid.uuid4().hex[:8]
        bucket_name = f"populis-portal-{aid}-{suffix}".lower()
        LOG.info("Bucket gerado: %s", bucket_name)

    zip_path: Path | None = None
    if not args.skip_lambda:
        if args.skip_packaging:
            zip_path = backend_dir / "function.zip"
            if not zip_path.is_file():
                LOG.error("Não encontrado: %s", zip_path)
                return 1
        elif args.use_docker:
            zip_path = build_lambda_zip_linux_docker(backend_dir, LOG)
        else:
            zip_path = build_lambda_zip_venv(backend_dir, LOG)
            LOG.warning(
                "ZIP gerado neste sistema operacional; na Lambda (Linux) pode falhar com wheels incompatíveis; "
                "prefira --use-docker para produção."
            )

    function_url = ""
    if not args.skip_lambda and zip_path:
        role_arn = ensure_iam_role(iam, args.role_name, LOG)
        create_or_update_lambda(
            lam,
            s3_client,
            args.lambda_name,
            role_arn,
            zip_path,
            args.region,
            args.runtime,
            LOG,
            staging_bucket=args.staging_bucket,
            staging_prefix="_lambda-deploy",
        )
        time.sleep(3)
        function_url = ensure_function_url(lam, args.lambda_name, LOG)

    config_write_path = (frontend_dir / "js" / "config.js") if args.write_local_config else None

    if not args.skip_s3 and bucket_name:
        create_bucket(s3_client, bucket_name, args.region, LOG)
        if args.public_website:
            configure_static_website(s3_client, bucket_name, LOG)

        api_base = function_url if function_url else ""
        sync_frontend(s3_client, bucket_name, frontend_dir, api_base, config_write_path, LOG)

    print()
    print("=== Deploy concluído ===")
    if bucket_name and not args.skip_s3:
        print(f"Bucket S3:       {bucket_name}")
        print(f"Console S3:      https://s3.console.aws.amazon.com/s3/buckets/{bucket_name}")
        if args.public_website:
            print(f"Website (HTTP):  {website_endpoint(bucket_name, args.region)}")
        else:
            print("Acesso HTTP:     bucket privado — use CloudFront/OAC ou --public-website.")
    if function_url:
        print(f"Lambda API URL:  {function_url}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
