# POPULIS AI — Demo de integração Amazon Bedrock

> Demo técnica do portal `portal/`: cada cliente usa **sua própria conta AWS** e uma **BEDROCK_API_KEY** gerada por ele. A plataforma consome a API compatível OpenAI do Bedrock (`bedrock-mantle.<região>.api.aws/v1`) **sem intermediar tokens** nem cobrar LLM por uso.

![Interface do portal — Chat simples com Bedrock](portal.png)

---

## Índice

- [Como funciona](#como-funciona)
- [Pré-requisitos](#pré-requisitos)
- [Estrutura do projeto](#estrutura-do-projeto)
- [Deploy rápido](#deploy-rápido)
- [Modelo de custos](#modelo-de-custos)
- [Escopo da POC](#escopo-da-poc)
- [Segurança](#segurança)

---

## Como funciona

O modelo é **BYO (Bring Your Own) chave + região**:

1. O cliente gera uma **Long-term API key** no AWS Console → Bedrock → API keys.
2. Informa a **BEDROCK_API_KEY** e a **região** (onde os modelos estão habilitados) na interface.
3. As chamadas vão **direto ao Bedrock** na conta do cliente — via Lambda + front estático neste repositório.
4. A **AWS cobra os tokens** na conta do cliente (visível no Cost Explorer / budgets).

O portal permite **listar modelos disponíveis** e **conversar** (chat com histórico, streaming por chunks e personalidade configurável).

---

## Pré-requisitos

- Conta AWS com **Amazon Bedrock habilitado** na região desejada.
- **Long-term API key** gerada em: AWS Console → Bedrock → API keys → Long-term API keys.
- Python 3.10+ e `pip` instalados localmente.
- Docker (recomendado para build do ZIP compatível com Lambda/Linux).

---

## Estrutura do projeto

```
portal/
├── frontend/        # UI POPULIS: região, chave, seleção de modelos, chat
├── backend/         # Lambda — POST /api/models  |  POST /api/completion
├── deploy_aws.py    # Empacota e publica Lambda + Function URL + bucket S3
└── scripts/         # Auxiliares: ZIP e s3 sync
```

Arquitetura de nuvem (Lambda, S3, CloudFront): [`../docs/DEPLOY_LAMBDA_CLOUDFRONT.md`](../docs/DEPLOY_LAMBDA_CLOUDFRONT.md).

---

## Deploy rápido

```bash
cd portal
pip install -r requirements-deploy.txt
python deploy_aws.py --region us-east-1 --use-docker --public-website --write-local-config
```

| Flag | Descrição |
|------|-----------|
| `--use-docker` | Gera ZIP compatível com Lambda (Linux) — **recomendado** |
| `--skip-s3` | Atualiza apenas a Lambda, sem tocar no S3 |
| `--public-website` | Configura bucket S3 como site estático público |
| `--write-local-config` | Salva configuração gerada localmente |

```bash
python deploy_aws.py --help   # lista todas as opções
```

---

## Modelo de custos

| Quem | O quê |
|------|--------|
| **Cliente** | Tokens de IA (Bedrock), faturados diretamente na conta AWS dele |
| **Populis AI** | Plataforma / produto (custo de tokens fora do escopo deste modelo) |

**Vantagens:** custo de modelo visível e controlado pelo cliente; limites e budgets AWS próprios; isolamento total por conta.

---

## Escopo da POC

✅ **Incluso nesta POC:**
- Modelo BYO chave + região.
- Demo web (front estático + API Lambda) com os fluxos principais do Bedrock.
- Apoio ao cliente na geração da chave no console (treinamento/orientação).

❌ **Fora do escopo da POC:**
- Cadastro persistente de chaves na plataforma Populis AI.
- Criptografia e validação formal de credenciais.
- Status de integração e tutorial embutido em produção.

> Itens fora do escopo exigem **Change Request** apartada com prazo e custo próprios.

---

## Segurança

A **Long-term API key** é uma credencial sensível. Boas práticas:

- **Não compartilhe** a chave fora do canal acordado com a Populis AI.
- Configure **expiração** da chave diretamente no console AWS.
- Faça **rotação periódica** da chave (AWS Console → Bedrock → API keys).
- Use **AWS Budgets** para limitar gastos inesperados na conta.
