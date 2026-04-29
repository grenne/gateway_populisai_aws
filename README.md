# POPULIS AI — Demo de integração Amazon Bedrock (`portal/`)

Demonstração técnica do **contexto de integração**: cada cliente usa **sua própria conta AWS** e uma **BEDROCK_API_KEY** gerada por ele; a plataforma apenas **consome a API compatível OpenAI do Bedrock** (`bedrock-mantle.<região>.api.aws/v1`), **sem intermediar tokens** nem cobrar LLM por uso.

---

## Como funciona

Cada cliente obtém no **AWS Console → Bedrock → API keys → Long-term API keys** uma chave de longa duração, informa **região** (onde os modelos estão habilitados) e usa esta demo para **listar modelos** e **conversar** (chat simples, histórico com chunks, personalidade). O consumo de tokens é **faturado pela AWS na conta do cliente**.

---

## Fluxo de uso

1. Console AWS → Bedrock → API keys → gerar chave de longo prazo.  
2. Informar **BEDROCK_API_KEY** e **região** na interface desta demo.  
3. Chamadas vão **direto ao Bedrock** na conta do cliente (neste repositório: via **Lambda** + front estático).  
4. **AWS cobra tokens** na conta do cliente (Cost Explorer / budget).

---

## Quem paga o quê

| Quem | O quê |
|------|--------|
| **Cliente** | Uso de IA (Bedrock), direto na conta AWS dele |
| **Populis AI** | Plataforma / produto (fora do escopo de custo de tokens neste modelo) |

**Vantagens do modelo:** custo de modelo **visível e na conta do cliente**; controle de gasto e limites AWS próprios; isolamento por conta.

---

## Escopo desta POC (este repositório)

- Orientação ao modelo **BYO chave + região**.  
- Demo web (**front** estático + **API** Lambda) reproduzindo fluxos de uso com Bedrock.  
- **Fora do escopo da POC:** *feature* na plataforma Populis AI com cadastro **persistente**, criptografia, validação formal, status da integração e tutorial embutido na produção — isso exige **Change Request** apartada (prazo/custo próprios).  
- Na POC, configuração da chave pode ser **manual**, com apoio ao cliente no console Bedrock (treinamento/orientação), alinhado ao texto de entregáveis da POC.

---

## Conteúdo do diretório `portal/`

| Item | Descrição |
|------|-----------|
| `frontend/` | UI POPULIS: região, chave, modelos, chat. |
| `backend/` | Lambda — `POST /api/models`, `POST /api/completion`. |
| `deploy_aws.py` | Opcional: empacota, publica Lambda + URL + bucket S3. |
| `scripts/` | Auxiliares ZIP e `s3 sync`. |

Arquitetura nuvem (Lambda, S3, CloudFront): **`../docs/DEPLOY_LAMBDA_CLOUDFRONT.md`**.

---

## Deploy rápido

```bash
cd portal
pip install -r requirements-deploy.txt
python deploy_aws.py --region us-east-1 --use-docker --public-website --write-local-config
```

`--use-docker` recomendado para ZIP compatível com Lambda (Linux). `--skip-s3` atualiza só a Lambda. Ver `python deploy_aws.py --help`.

---

## Segurança

Chave **Long-term** é credencial sensível: expiração no console AWS, rotação e **não** compartilhar fora do canal acordado com a Populis AI.
