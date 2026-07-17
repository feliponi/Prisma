# Prisma — Gestão Financeira Pessoal (MVP local)

Aplicação local para importar extratos bancários e de cartão de crédito em
CSV (de qualquer banco), sanitizar os dados, categorizá-los com um LLM local
(via Ollama) e gerar insights de orçamento. Multi-conta, multi-moeda
(BRL + EUR, sem conversão entre elas), 100% local — nenhum dado sai da sua
máquina.

> **Status atual: Fase 1 (contratos).** As assinaturas de todas as funções,
> os modelos de dados, o schema do SQLite e o fixture de teste (golden
> sample) já estão prontos e versionados, mas os *corpos* das funções em
> `csv_mapper.py`, `ai_services.py`, `db.py` e `app.py` ainda levantam
> `NotImplementedError` — a implementação (Fase 2) ainda não foi feita.
> As instruções abaixo cobrem como preparar o ambiente hoje e como a
> aplicação será executada assim que a Fase 2 estiver pronta.

## Pré-requisitos

- **Python 3.11+**
- **[Ollama](https://ollama.com)** instalado e rodando localmente
  (`ollama serve`), com uma GPU de pelo menos 12 GB de VRAM recomendada.
- O modelo `qwen2.5:7b-instruct` (quantização Q4_K_M) baixado no Ollama:

  ```bash
  ollama pull qwen2.5:7b-instruct
  ```

## Instalação

```bash
# 1. Clone o repositório e entre na pasta
git clone <url-do-repositorio>
cd Prisma

# 2. Crie e ative um ambiente virtual
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# 3. Instale as dependências
pip install -r requirements.txt
```

## Como rodar

1. Garanta que o Ollama está em execução em outro terminal:

   ```bash
   ollama serve
   ```

2. Suba a interface Streamlit:

   ```bash
   streamlit run app.py
   ```

3. Acesse `http://localhost:8501` no navegador. O fluxo na interface é:

   **Selecionar/Criar Conta → Upload do CSV → Mapear Colunas → Pré-visualizar
   → Categorizar com IA → Ver Insights**

   - Ao criar uma conta pela primeira vez, você mapeia as colunas do CSV do
     seu banco para o modelo canônico e define o tipo de conta
     (`bank_account` ou `credit_card`), a moeda padrão, o formato de data,
     os separadores decimal/milhar e o padrão de sinal do valor. Esse
     mapeamento é salvo em `mappings/{account_id}_config.json` e reaplicado
     automaticamente nas próximas importações do mesmo banco.
   - Reimportar o mesmo extrato (ou um com sobreposição de datas) não gera
     duplicatas: a gravação no SQLite é idempotente via hash da transação.

## Configuração do banco de dados local

Nenhum passo manual é necessário: o schema (`schema.sql`) e a taxonomia de
categorias (`categories.json`) são aplicados automaticamente na primeira
execução, criando o arquivo `finance.db` na raiz do projeto (ignorado pelo
Git).

## Estrutura do projeto

```
models.py                   # Modelos de dados canônicos (dataclasses, enums, TypedDicts)
schema.sql                  # DDL do SQLite (accounts, categories, transactions)
categories.json             # Taxonomia de categorias (fonte única para DB e LLM)
text_utils.py               # Normalização de descrição (usada no hash e no cache de IA)
csv_mapper.py               # Perfis de conta + sanitização de CSV
db.py                       # Persistência SQLite (import idempotente)
ai_services.py              # Integração com Ollama (categorização + insights)
app.py                      # Interface Streamlit (em pt_BR)
mappings/                   # Perfis de mapeamento salvos por conta (JSON)
tests/golden_sample/        # Fixture de aceitação: CSVs sujos + saída esperada
```

## Rodando o fixture de teste (golden sample)

`tests/golden_sample/` contém extratos "sujos" de exemplo (separadores
decimais BR e EUR, coluna de valor invertida em cartão de crédito, linha de
transferência interna, rodapé de saldo embutido, linha vazia, coluna de
moeda por linha, etc.) junto com a saída canônica esperada, incluindo os
hashes de transação já calculados. Depois que a Fase 2 estiver implementada,
valide com:

```bash
python -m pytest tests/ -v
```

(ainda não há um arquivo `test_*.py` formal — `tests/golden_sample/README.md`
explica como comparar a saída de `csv_mapper.process_csv` com o fixture.)

## Notas importantes

- **Sem conversão de moeda.** BRL e EUR são sempre exibidos lado a lado,
  nunca somados ou convertidos.
- **Transferências internas** (ex.: pagamento de fatura de cartão) são
  identificadas por regex por conta e excluídas dos insights de gastos para
  evitar contagem duplicada — não há reconciliação automática por
  valor/data entre conta e cartão.
- **Processamento de IA é serial.** Como o Ollama roda em uma única GPU,
  todas as chamadas de categorização e geração de insights são feitas uma
  de cada vez, nunca em paralelo.
