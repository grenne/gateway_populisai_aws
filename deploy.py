#!/usr/bin/env python3
"""
Deploy do front-end POPULIS (pasta ``portal/frontend/``) para S3 + CloudFront com OAC.

Executar na pasta ``portal``::

    pip install boto3
    python deploy.py --api-url "https://xxxx.lambda-url.us-east-1.on.aws"

Variáveis úteis: ``FRONTEND_S3_BUCKET``, ``PORTAL_API_URL``, ``AWS_REGION``.
``FRONTEND_AWS_ACCOUNT_ID``: se definida, o deploy falha se a conta do ``aws sts`` for outra.
Por padrão a distribuição CloudFront existente é **reutilizada**; use ``--force-recreate-cloudfront``
ou ``FORCE_RECREATE_CLOUDFRONT=true`` para recriar.
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

import boto3
from botocore.exceptions import ClientError

# Bucket S3 padrão (sobrescreva com FRONTEND_S3_BUCKET ou ``--bucket``).
DEFAULT_S3_BUCKET = "populis-portal-691423932902-f2a87192"

# Raiz do portal (diretório que contém este script).
PORTAL_ROOT = Path(__file__).resolve().parent
FRONTEND_DIR = PORTAL_ROOT / "frontend"

# Cores para output
class Colors:
    GREEN = '\033[0;32m'
    BLUE = '\033[0;34m'
    RED = '\033[0;31m'
    YELLOW = '\033[1;33m'
    NC = '\033[0m'  # No Color

def print_colored(message: str, color: str = Colors.NC):
    """Imprime mensagem colorida"""
    print(f"{color}{message}{Colors.NC}")

def print_header(title: str):
    """Imprime cabeçalho formatado"""
    print_colored("━" * 40, Colors.BLUE)
    print_colored(title, Colors.BLUE)
    print_colored("━" * 40, Colors.BLUE)
    print()

def check_aws_credentials():
    """Verifica se credenciais AWS estão configuradas"""
    try:
        sts = boto3.client('sts')
        identity = sts.get_caller_identity()
        return identity.get('Account')
    except Exception as e:
        print_colored("❌ Erro: Credenciais AWS não configuradas!", Colors.RED)
        print_colored(f"   {str(e)}", Colors.YELLOW)
        print_colored("   Execute: aws configure", Colors.YELLOW)
        return None

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Publica portal/frontend no S3 e configura CloudFront (OAC)."
    )
    p.add_argument(
        "--frontend-dir",
        type=Path,
        default=None,
        help="Pasta do site estático (padrão: portal/frontend ao lado deste script).",
    )
    p.add_argument(
        "--api-url",
        default=os.environ.get("PORTAL_API_URL", "").strip(),
        help="URL da Lambda Function URL (sem barra final); injeta em js/config.js no upload.",
    )
    p.add_argument(
        "--bucket",
        default=(os.getenv("FRONTEND_S3_BUCKET") or "").strip() or DEFAULT_S3_BUCKET,
        help="Bucket S3 de destino.",
    )
    p.add_argument(
        "--region",
        default=os.getenv("AWS_REGION", "us-east-1"),
        help="Região do bucket S3 (e sessão boto3).",
    )
    p.add_argument(
        "--force-recreate-cloudfront",
        action="store_true",
        help="Remove e recria a distribuição CloudFront guardada em .cloudfront-config.json.",
    )
    p.add_argument(
        "--skip-cloudfront",
        action="store_true",
        help="Apenas envia arquivos ao S3 (sem criar OAC/CloudFront). Útil se a infra já existe.",
    )
    p.add_argument(
        "--write-local-config",
        action="store_true",
        help="Grava js/config.js localmente com --api-url antes do upload.",
    )
    return p.parse_args()


def build_config(args: argparse.Namespace) -> Dict[str, Any]:
    frontend_dir = Path(args.frontend_dir).resolve() if args.frontend_dir else FRONTEND_DIR
    force_recreate = args.force_recreate_cloudfront
    if os.getenv("FORCE_RECREATE_CLOUDFRONT", "").strip().lower() == "true":
        force_recreate = True
    return {
        "s3_bucket": args.bucket.strip() or DEFAULT_S3_BUCKET,
        "aws_region": args.region,
        "stage": os.getenv("STAGE", "dev"),
        "force_recreate": force_recreate,
        "delete_distribution_id": os.getenv("DELETE_DISTRIBUTION_ID", ""),
        "api_url": (args.api_url or "").strip().rstrip("/"),
        "skip_cloudfront": args.skip_cloudfront,
        "write_local_config": args.write_local_config,
        "frontend_dir": frontend_dir,
    }


def validate_frontend(frontend_dir: Path) -> bool:
    """Valida estrutura esperada do front POPULIS."""
    required = [
        frontend_dir / "index.html",
        frontend_dir / "js" / "app.js",
        frontend_dir / "js" / "config.js",
        frontend_dir / "css" / "main.css",
    ]
    missing = [p for p in required if not p.is_file()]
    if missing:
        print_colored("❌ Arquivos essenciais não encontrados em:", Colors.RED)
        print_colored(f"   {frontend_dir}", Colors.YELLOW)
        for p in missing:
            print(f"   - {p.relative_to(frontend_dir)}")
        return False
    print_colored(f"✅ Frontend OK: {frontend_dir}", Colors.GREEN)
    return True


def patch_config_js(content: str, api_base: str) -> str:
    """Substitui window.PORTAL_API_BASE em js/config.js."""
    escaped = json.dumps(api_base)
    new_content, n = re.subn(
        r"window\.PORTAL_API_BASE\s*=\s*[^;]+;",
        f"window.PORTAL_API_BASE = {escaped};",
        content,
        count=1,
    )
    if n == 0:
        print_colored(
            "⚠️  Linha window.PORTAL_API_BASE não encontrada em config.js — upload sem injeção.",
            Colors.YELLOW,
        )
        return content
    return new_content

def get_content_type(filename: str) -> str:
    """Determina o content-type baseado na extensão do arquivo"""
    ext = Path(filename).suffix.lower()
    content_types = {
        '.js': 'application/javascript',
        '.css': 'text/css',
        '.html': 'text/html',
        '.json': 'application/json',
        '.png': 'image/png',
        '.jpg': 'image/jpeg',
        '.jpeg': 'image/jpeg',
        '.svg': 'image/svg+xml',
    }
    return content_types.get(ext, 'application/octet-stream')

def setup_s3_bucket(s3_client, bucket_name: str, region: str):
    """Configura o bucket S3 (cria se não existir, configura versionamento e bloqueio público)"""
    print_header("🪣 ETAPA 2: Configuração do S3")
    
    # Verificar se bucket existe
    try:
        s3_client.head_bucket(Bucket=bucket_name)
        print_colored(f"✅ Bucket encontrado: {bucket_name}", Colors.GREEN)
    except ClientError as e:
        error_code = e.response.get('Error', {}).get('Code', '')
        if error_code == '404':
            # Bucket não existe: criar
            print_colored(f"⚠️  Bucket {bucket_name} não encontrado.", Colors.YELLOW)
            print_colored(f"🆕 Criando bucket {bucket_name}...", Colors.BLUE)
            
            try:
                if region == 'us-east-1':
                    s3_client.create_bucket(Bucket=bucket_name)
                else:
                    s3_client.create_bucket(
                        Bucket=bucket_name,
                        CreateBucketConfiguration={'LocationConstraint': region}
                    )
                print_colored(f"✅ Bucket {bucket_name} criado!", Colors.GREEN)
            except ClientError as create_error:
                print_colored(f"❌ Erro ao criar bucket: {create_error}", Colors.RED)
                return False
        elif error_code == '403' or (e.response.get('ResponseMetadata', {}).get('HTTPStatusCode') == 403):
            print_colored(f"❌ Erro ao verificar bucket: {e}", Colors.RED)
            print_colored("   Forbidden (403): sem permissão para este bucket.", Colors.YELLOW)
            print_colored("   • Confirme o nome do bucket e a conta AWS.", Colors.YELLOW)
            print_colored("   • IAM precisa de s3:HeadBucket, s3:ListBucket, s3:PutObject.", Colors.YELLOW)
            print_colored(f"   • Ajuste FRONTEND_S3_BUCKET ou ``--bucket``.", Colors.YELLOW)
            return False
        else:
            print_colored(f"❌ Erro ao verificar bucket: {e}", Colors.RED)
            return False
    
    # Configurar versionamento
    try:
        s3_client.put_bucket_versioning(
            Bucket=bucket_name,
            VersioningConfiguration={'Status': 'Enabled'}
        )
    except ClientError as e:
        print_colored(f"⚠️  Aviso: Não foi possível configurar versionamento: {e}", Colors.YELLOW)
    
    # Bloquear acesso público
    try:
        s3_client.put_public_access_block(
            Bucket=bucket_name,
            PublicAccessBlockConfiguration={
                'BlockPublicAcls': True,
                'IgnorePublicAcls': True,
                'BlockPublicPolicy': True,
                'RestrictPublicBuckets': True
            }
        )
    except ClientError as e:
        print_colored(f"⚠️  Aviso: Não foi possível bloquear acesso público: {e}", Colors.YELLOW)
    
    print()
    return True

def upload_frontend_to_s3(
    s3_client,
    bucket_name: str,
    frontend_dir: Path,
    api_url: str,
    write_local_config: bool,
) -> bool:
    """Envia ``frontend_dir`` para S3 preservando pastas (css/, js/). Injeta API em config.js."""
    print_colored("📤 Enviando frontend para S3...", Colors.BLUE)

    cfg_rel = Path("js/config.js")
    cfg_path = frontend_dir / cfg_rel
    patched_txt: Optional[str] = None
    if cfg_path.is_file():
        raw = cfg_path.read_text(encoding="utf-8")
        if api_url:
            patched_txt = patch_config_js(raw, api_url)
            print_colored(f"   🔗 API base no bundle: {api_url}", Colors.BLUE)
        else:
            patched_txt = raw
            print_colored(
                "   ℹ️  Sem --api-url: config.js enviado como está (meta ou proxy no mesmo domínio).",
                Colors.YELLOW,
            )
        if write_local_config:
            cfg_path.write_text(patched_txt, encoding="utf-8")
            print_colored(f"   💾 Gravado localmente: {cfg_path}", Colors.GREEN)

    ok = True
    for fp in sorted(frontend_dir.rglob("*")):
        if fp.is_dir():
            continue
        rel = fp.relative_to(frontend_dir).as_posix()
        ext = fp.suffix.lower()
        if ext in (".html",):
            cache = "no-cache, no-store, must-revalidate"
        elif ext in (".js", ".css", ".woff", ".woff2"):
            cache = "public, max-age=31536000, immutable"
        else:
            cache = "public, max-age=86400"

        content_type = get_content_type(fp.name)
        try:
            if patched_txt is not None and rel == cfg_rel.as_posix():
                body = patched_txt.encode("utf-8")
                s3_client.put_object(
                    Bucket=bucket_name,
                    Key=rel,
                    Body=body,
                    ContentType=content_type,
                    CacheControl=cache,
                )
            else:
                extra = {"ContentType": content_type, "CacheControl": cache}
                s3_client.upload_file(str(fp), bucket_name, rel, ExtraArgs=extra)
            print_colored(f"   ✅ {rel}", Colors.GREEN)
        except Exception as e:
            print_colored(f"   ❌ {rel}: {e}", Colors.RED)
            ok = False

    if ok:
        print_colored("✅ Upload do frontend concluído.", Colors.GREEN)
    print()
    return ok

def get_or_create_oac(cloudfront_client, oac_config_file: str) -> Optional[str]:
    """Obtém ou cria Origin Access Control"""
    print_header("🔐 ETAPA 3: Origin Access Control (OAC)")
    
    # Verificar se OAC já existe no arquivo de configuração
    existing_oac_id = None
    if Path(oac_config_file).exists():
        try:
            with open(oac_config_file, 'r') as f:
                oac_data = json.load(f)
                existing_oac_id = oac_data.get('Id')
        except:
            pass
    
    if existing_oac_id:
        # Verificar se OAC ainda existe
        try:
            cloudfront_client.get_origin_access_control(Id=existing_oac_id)
            print_colored(f"ℹ️  OAC já existe: {existing_oac_id}", Colors.YELLOW)
            return existing_oac_id
        except:
            print_colored("⚠️  OAC salvo não existe mais. Criando novo...", Colors.YELLOW)
    
    # Criar novo OAC
    print_colored("🆕 Criando Origin Access Control...", Colors.BLUE)
    oac_name = f"portal-demo-oac-{int(time.time())}"
    
    try:
        response = cloudfront_client.create_origin_access_control(
            OriginAccessControlConfig={
                'Name': oac_name,
                'Description': 'OAC for demo frontend (S3 static)',
                'SigningProtocol': 'sigv4',
                'SigningBehavior': 'always',
                'OriginAccessControlOriginType': 's3'
            }
        )
        oac_id = response['OriginAccessControl']['Id']
        
        # Salvar configuração
        with open(oac_config_file, 'w') as f:
            json.dump({'Id': oac_id}, f, indent=2)
        
        print_colored(f"✅ OAC criado: {oac_id}", Colors.GREEN)
        print()
        return oac_id
    except Exception as e:
        print_colored(f"❌ Erro ao criar OAC: {e}", Colors.RED)
        print_colored("   Continue manualmente pelo Console AWS", Colors.YELLOW)
        return None

def delete_cloudfront_distribution(cloudfront_client, distribution_id: str):
    """Deleta uma distribuição CloudFront existente"""
    print_header("🗑️  Deletando Distribuição CloudFront Existente")
    
    try:
        # Obter distribuição atual
        response = cloudfront_client.get_distribution(Id=distribution_id)
        dist = response['Distribution']
        etag = response['ETag']
        status = dist['Status']
        
        print_colored(f"📋 Distribuição encontrada: {distribution_id}", Colors.BLUE)
        print_colored(f"   Status atual: {status}", Colors.BLUE)
        print_colored(f"   Domain: {dist.get('DomainName', 'N/A')}", Colors.BLUE)
        
        # Se estiver habilitada, desabilitar primeiro
        if dist['DistributionConfig']['Enabled']:
            print_colored("⚠️  Distribuição está habilitada. Desabilitando...", Colors.YELLOW)
            
            # Obter configuração atual
            config_response = cloudfront_client.get_distribution_config(Id=distribution_id)
            config = config_response['DistributionConfig']
            config_etag = config_response['ETag']
            
            # Desabilitar
            config['Enabled'] = False
            
            cloudfront_client.update_distribution(
                Id=distribution_id,
                DistributionConfig=config,
                IfMatch=config_etag
            )
            
            print_colored("⏳ Aguardando distribuição ser desabilitada...", Colors.YELLOW)
            
            # Aguardar até estar desabilitada (pode levar alguns minutos)
            max_wait = 300  # 5 minutos
            wait_time = 0
            while wait_time < max_wait:
                time.sleep(10)
                wait_time += 10
                
                check_response = cloudfront_client.get_distribution(Id=distribution_id)
                check_status = check_response['Distribution']['Status']
                
                print_colored(f"   Status: {check_status} (aguardando {wait_time}s)", Colors.BLUE)
                
                if check_status == 'Deployed':
                    break
            
            if wait_time >= max_wait:
                print_colored("⚠️  Timeout aguardando desabilitação. Tente deletar manualmente.", Colors.YELLOW)
                return False
        
        # Obter ETag atualizado para deletar
        final_response = cloudfront_client.get_distribution(Id=distribution_id)
        final_etag = final_response['ETag']
        
        # Deletar distribuição
        print_colored("🗑️  Deletando distribuição...", Colors.BLUE)
        cloudfront_client.delete_distribution(
            Id=distribution_id,
            IfMatch=final_etag
        )
        
        print_colored(f"✅ Distribuição {distribution_id} deletada com sucesso!", Colors.GREEN)
        print()
        return True
        
    except ClientError as e:
        error_code = e.response.get('Error', {}).get('Code', '')
        if error_code == 'NoSuchDistribution':
            print_colored(f"ℹ️  Distribuição {distribution_id} não existe mais", Colors.YELLOW)
            print()
            return True
        else:
            print_colored(f"❌ Erro ao deletar distribuição: {e}", Colors.RED)
            print_colored("   Delete manualmente pelo Console AWS", Colors.YELLOW)
            print()
            return False
    except Exception as e:
        print_colored(f"❌ Erro ao deletar distribuição: {e}", Colors.RED)
        print()
        return False

def get_or_create_cloudfront_distribution(
    cloudfront_client, 
    s3_bucket: str, 
    region: str, 
    oac_id: str,
    cloudfront_config_file: str,
    aws_account_id: str,
    force_recreate: bool = False,
    delete_distribution_id: str = ''
) -> Optional[Dict[str, Any]]:
    """Obtém ou cria distribuição CloudFront"""
    print_header("☁️  ETAPA 4: Configuração do CloudFront")
    
    # Se há um ID específico para deletar, deletar primeiro
    if delete_distribution_id:
        print_colored(f"🗑️  Deletando distribuição especificada: {delete_distribution_id}", Colors.YELLOW)
        delete_cloudfront_distribution(cloudfront_client, delete_distribution_id)
        # Remover arquivo de configuração se existir
        if Path(cloudfront_config_file).exists():
            try:
                Path(cloudfront_config_file).unlink()
                print_colored("🗑️  Arquivo de configuração removido", Colors.BLUE)
            except:
                pass
    
    # Verificar se distribuição já existe
    existing_dist_id = None
    if Path(cloudfront_config_file).exists() and not force_recreate:
        try:
            with open(cloudfront_config_file, 'r') as f:
                dist_data = json.load(f)
                existing_dist_id = dist_data.get('Id')
        except:
            pass
    
    # Se force_recreate, deletar distribuição existente
    if existing_dist_id and force_recreate:
        print_colored(f"🔄 Modo force_recreate ativado. Deletando distribuição existente...", Colors.YELLOW)
        delete_cloudfront_distribution(cloudfront_client, existing_dist_id)
        existing_dist_id = None
        # Remover arquivo de configuração
        if Path(cloudfront_config_file).exists():
            try:
                Path(cloudfront_config_file).unlink()
            except:
                pass
    
    if existing_dist_id:
        try:
            response = cloudfront_client.get_distribution(Id=existing_dist_id)
            dist = response['Distribution']
            print_colored(f"ℹ️  Distribuição CloudFront já existe: {existing_dist_id}", Colors.YELLOW)
            return {
                'Id': existing_dist_id,
                'DomainName': dist['DomainName'],
                'ARN': dist['ARN']
            }
        except:
            print_colored("⚠️  Distribuição salva não existe mais. Criando nova...", Colors.YELLOW)
    
    # Criar nova distribuição
    print_colored("🆕 Criando distribuição CloudFront com OAC...", Colors.BLUE)
    print()
    
    s3_domain_name = f"{s3_bucket}.s3.{region}.amazonaws.com"
    caller_reference = f"portal-demo-{int(time.time())}"
    
    distribution_config = {
        'CallerReference': caller_reference,
        'Comment': 'Demo frontend distribution (OAC + S3)',
        'Enabled': True,
        'Origins': {
            'Quantity': 1,
            'Items': [{
                'Id': f'S3-{s3_bucket}',
                'DomainName': s3_domain_name,
                'OriginAccessControlId': oac_id,
                'S3OriginConfig': {
                    'OriginAccessIdentity': ''
                }
            }]
        },
        'DefaultRootObject': 'index.html',
        'DefaultCacheBehavior': {
            'TargetOriginId': f'S3-{s3_bucket}',
            'ViewerProtocolPolicy': 'redirect-to-https',
            'AllowedMethods': {
                'Quantity': 2,
                'Items': ['GET', 'HEAD'],
                'CachedMethods': {
                    'Quantity': 2,
                    'Items': ['GET', 'HEAD']
                }
            },
            'Compress': True,
            'CachePolicyId': '658327ea-f89d-4fab-a63d-7e88639e58f6',
            'OriginRequestPolicyId': '88a5eaf4-2fd4-4709-b370-b4c650ea3fcf'
        },
        'CustomErrorResponses': {
            'Quantity': 2,
            'Items': [
                {
                    'ErrorCode': 403,
                    'ResponsePagePath': '/index.html',
                    'ResponseCode': '200',
                    'ErrorCachingMinTTL': 0
                },
                {
                    'ErrorCode': 404,
                    'ResponsePagePath': '/index.html',
                    'ResponseCode': '200',
                    'ErrorCachingMinTTL': 0
                }
            ]
        },
        'PriceClass': 'PriceClass_100'
    }
    
    try:
        response = cloudfront_client.create_distribution(
            DistributionConfig=distribution_config
        )
        dist = response['Distribution']
        dist_id = dist['Id']
        dist_domain = dist['DomainName']
        dist_arn = dist['ARN']
        
        # Salvar configuração
        with open(cloudfront_config_file, 'w') as f:
            json.dump({
                'Id': dist_id,
                'DomainName': dist_domain,
                'ARN': dist_arn,
                'Status': dist['Status']
            }, f, indent=2)
        
        print_colored("✅ Distribuição CloudFront criada!", Colors.GREEN)
        print_colored(f"   Distribution ID: {dist_id}", Colors.BLUE)
        print_colored(f"   Domain: {dist_domain}", Colors.BLUE)
        print_colored(f"   ARN: {dist_arn}", Colors.BLUE)
        print()
        
        return {
            'Id': dist_id,
            'DomainName': dist_domain,
            'ARN': dist_arn
        }
    except Exception as e:
        print_colored(f"❌ Erro ao criar distribuição CloudFront: {e}", Colors.RED)
        print_colored("   Continue manualmente pelo Console AWS", Colors.YELLOW)
        return None

def update_s3_bucket_policy(s3_client, bucket_name: str, distribution_arn: str):
    """Atualiza política do bucket S3 para permitir acesso via CloudFront"""
    print_header("🔒 ETAPA 5: Atualizar Política do S3")
    
    print_colored("📝 Criando política do bucket para CloudFront...", Colors.BLUE)
    
    bucket_policy = {
        "Version": "2008-10-17",
        "Id": "PolicyForCloudFrontPrivateContent",
        "Statement": [
            {
                "Sid": "AllowCloudFrontServicePrincipal",
                "Effect": "Allow",
                "Principal": {
                    "Service": "cloudfront.amazonaws.com"
                },
                "Action": "s3:GetObject",
                "Resource": f"arn:aws:s3:::{bucket_name}/*",
                "Condition": {
                    "StringEquals": {
                        "AWS:SourceArn": distribution_arn
                    }
                }
            }
        ]
    }
    
    try:
        s3_client.put_bucket_policy(
            Bucket=bucket_name,
            Policy=json.dumps(bucket_policy)
        )
        print_colored("✅ Política do S3 atualizada!", Colors.GREEN)
        print_colored(f"   Bucket: {bucket_name}", Colors.BLUE)
        print_colored(f"   CloudFront ARN: {distribution_arn}", Colors.BLUE)
        print()
        return True
    except Exception as e:
        print_colored(f"❌ Erro ao atualizar política do S3: {e}", Colors.RED)
        print_colored("   Aplique manualmente pelo Console AWS", Colors.YELLOW)
        print()
        return False

def invalidate_cloudfront_cache(cloudfront_client, distribution_id: str):
    """Invalida cache do CloudFront"""
    print_header("🔄 ETAPA 6: Invalidação de Cache")
    
    print_colored("🔄 Invalidando cache do CloudFront...", Colors.BLUE)
    
    try:
        response = cloudfront_client.create_invalidation(
            DistributionId=distribution_id,
            InvalidationBatch={
                'Paths': {
                    'Quantity': 1,
                    'Items': ['/*']
                },
                'CallerReference': f"portal-demo-{int(time.time())}"
            }
        )
        invalidation_id = response['Invalidation']['Id']
        print_colored("✅ Cache invalidado!", Colors.GREEN)
        print_colored(f"   Invalidation ID: {invalidation_id}", Colors.BLUE)
        print()
        return True
    except Exception as e:
        print_colored(f"⚠️  Aguarde alguns minutos e tente invalidar manualmente: {e}", Colors.YELLOW)
        print()
        return False

def print_final_info(
    config: Dict[str, Any],
    oac_id: Optional[str],
    distribution: Optional[Dict],
    *,
    s3_only: bool = False,
) -> None:
    """Imprime informações finais do deploy."""
    print_colored("━" * 40, Colors.GREEN)
    print_colored("✅ Deploy concluído", Colors.GREEN)
    print_colored("━" * 40, Colors.GREEN)
    print()

    print_colored("📊 Resumo:", Colors.BLUE)
    print()

    print_colored("🪣 S3 Bucket:", Colors.BLUE)
    print_colored(f"   Nome: {config['s3_bucket']}", Colors.NC)
    print_colored(f"   Região: {config['aws_region']}", Colors.NC)
    if s3_only:
        print_colored("   Modo deste run: somente upload S3 (--skip-cloudfront)", Colors.NC)
    else:
        print_colored("   Modo: privado — leitura via CloudFront (OAC)", Colors.NC)
    print()

    if oac_id:
        print_colored("🔐 Origin Access Control:", Colors.BLUE)
        print_colored(f"   OAC ID: {oac_id}", Colors.NC)
        print_colored(f"   Config: {PORTAL_ROOT / '.oac-config.json'}", Colors.NC)
        print()

    if distribution:
        print_colored("☁️  CloudFront:", Colors.BLUE)
        print_colored(f"   Distribution ID: {distribution['Id']}", Colors.NC)
        print_colored(f"   ARN: {distribution['ARN']}", Colors.NC)
        if distribution.get("DomainName"):
            print_colored(f"   URL: https://{distribution['DomainName']}", Colors.NC)
            print()
            print_colored("🌐 URL da aplicação:", Colors.GREEN)
            print_colored(f"   https://{distribution['DomainName']}", Colors.GREEN)
        print()
        print_colored(f"💾 Config: {PORTAL_ROOT / '.cloudfront-config.json'}", Colors.BLUE)

    print()
    print_colored("📝 Próximos passos:", Colors.BLUE)
    if distribution:
        print_colored("   1. No console CloudFront, aguarde Status = Deployed (~5–15 min na 1ª vez).", Colors.NC)
        print_colored("   2. Abra a URL HTTPS acima e teste chat + API.", Colors.NC)
    elif s3_only:
        print_colored("   1. Distribua pelo CloudFront já existente ou rode sem --skip-cloudfront.", Colors.NC)
        print_colored("   2. Se já tinha CloudFront, invalidação foi disparada (quando havia .cloudfront-config.json).", Colors.NC)
    else:
        print_colored("   1. Revise erros acima (OAC / CloudFront).", Colors.NC)
        print_colored("   2. Corrija permissões IAM e execute novamente.", Colors.NC)

    print()
    print_colored("🔒 Segurança:", Colors.BLUE)
    print_colored("   ✅ Bucket S3 sem acesso público direto", Colors.GREEN)
    if not s3_only:
        print_colored("   ✅ CloudFront OAC + HTTPS", Colors.GREEN)
        print_colored("   ✅ Política do bucket amarrada ao ARN da distribuição", Colors.GREEN)

    print()
    print_colored("━" * 40, Colors.GREEN)
    print_colored("🚀 PRONTO PARA SUBIR / TESTAR", Colors.GREEN)
    print_colored("━" * 40, Colors.GREEN)
    if distribution and distribution.get("DomainName"):
        print_colored(
            "   → Use: https://" + distribution["DomainName"],
            Colors.GREEN,
        )
        print_colored("   → Garanta que --api-url aponta para a Lambda Function URL ativa.", Colors.NC)
    elif s3_only:
        print_colored(
            "   → Front atualizado no S3; acesse pelo domínio CloudFront que você já usa.",
            Colors.GREEN,
        )
    print()


def main() -> None:
    print_header("POPULIS — frontend → S3 + CloudFront (OAC)")
    print_colored(f"Diretório do projeto: {PORTAL_ROOT}", Colors.BLUE)
    print()

    args = parse_args()
    config = build_config(args)

    aws_account_id = check_aws_credentials()
    if not aws_account_id:
        sys.exit(1)

    expected_account = os.getenv("FRONTEND_AWS_ACCOUNT_ID", "").strip()
    if expected_account and aws_account_id != expected_account:
        print_colored(
            f"❌ Conta AWS atual ({aws_account_id}) ≠ FRONTEND_AWS_ACCOUNT_ID ({expected_account}).",
            Colors.RED,
        )
        sys.exit(1)

    os.chdir(PORTAL_ROOT)

    print_colored(f"📦 Stage: {config['stage']}", Colors.BLUE)
    print_colored(f"🪣 Bucket: {config['s3_bucket']}", Colors.BLUE)
    print_colored(f"📁 Frontend: {config['frontend_dir']}", Colors.BLUE)
    if config["force_recreate"]:
        print_colored("🔄 CloudFront será recriado (force).", Colors.YELLOW)
    else:
        print_colored("ℹ️  CloudFront existente será reutilizado se houver .cloudfront-config.json.", Colors.BLUE)
    print()

    print_header("📋 ETAPA 1: Validação")
    if not validate_frontend(config["frontend_dir"]):
        sys.exit(1)

    s3_client = boto3.client("s3", region_name=config["aws_region"])
    cloudfront_client = boto3.client("cloudfront", region_name="us-east-1")

    if not setup_s3_bucket(s3_client, config["s3_bucket"], config["aws_region"]):
        sys.exit(1)

    if not upload_frontend_to_s3(
        s3_client,
        config["s3_bucket"],
        config["frontend_dir"],
        config["api_url"],
        config["write_local_config"],
    ):
        sys.exit(1)

    oac_path = str(PORTAL_ROOT / ".oac-config.json")
    cf_path = str(PORTAL_ROOT / ".cloudfront-config.json")

    if config["skip_cloudfront"]:
        print_colored("⏭️  ETAPA 3–6: ignoradas (--skip-cloudfront).", Colors.YELLOW)
        distribution: Optional[Dict[str, Any]] = None
        if Path(cf_path).exists():
            try:
                dist_id = json.loads(Path(cf_path).read_text(encoding="utf-8")).get("Id")
                if dist_id:
                    invalidate_cloudfront_cache(cloudfront_client, dist_id)
            except Exception as exc:
                print_colored(f"⚠️  Invalidação opcional falhou: {exc}", Colors.YELLOW)
        print_final_info(config, None, None, s3_only=True)
        return

    oac_id = get_or_create_oac(cloudfront_client, oac_path)
    if not oac_id:
        print_colored("❌ OAC obrigatório para CloudFront + S3 privado. Abortando.", Colors.RED)
        sys.exit(1)

    distribution = get_or_create_cloudfront_distribution(
        cloudfront_client,
        config["s3_bucket"],
        config["aws_region"],
        oac_id,
        cf_path,
        aws_account_id,
        force_recreate=config.get("force_recreate", False),
        delete_distribution_id=config.get("delete_distribution_id", ""),
    )

    if distribution:
        update_s3_bucket_policy(s3_client, config["s3_bucket"], distribution["ARN"])
        invalidate_cloudfront_cache(cloudfront_client, distribution["Id"])

    print_final_info(config, oac_id, distribution, s3_only=False)

if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print_colored("\n\n⚠️  Deploy cancelado pelo usuário", Colors.YELLOW)
        sys.exit(1)
    except Exception as e:
        print_colored(f"\n❌ Erro inesperado: {str(e)}", Colors.RED)
        import traceback
        traceback.print_exc()
        sys.exit(1)
