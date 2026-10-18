# Catalog plan: every tool id in Topolearn

Use these exact ids. A tool may name any tool below in its `tool.requires`
(tool-level prerequisite, e.g. `{"tool": "python", "level": 1}`), even if its
file is written by someone else.

Lesson-level `requires` may point at another tool's lessons ONLY if that tool's
file already exists in `data/tools/` (read it to get real lesson ids) or you write
that tool yourself, and that tool is in your `tool.requires`.

## Existing (files already written)
python, javascript, typescript, java, sql, go, rust, cpp, react, nodejs, express,
fastapi, numpy, pandas, git, docker

## New

### Python standard library (category library; requires python)
py-os (name "os"), py-sys ("sys"), py-json ("json"), py-pathlib ("pathlib"),
py-re ("re"), py-datetime ("datetime"), py-collections ("collections"),
py-itertools ("itertools"), py-functools ("functools"), py-asyncio ("asyncio"),
py-logging ("logging"), py-subprocess ("subprocess"), py-argparse ("argparse"),
py-typing ("typing"), py-unittest ("unittest"), py-csv ("csv"),
py-concurrency ("threading & multiprocessing"), py-dataclasses ("dataclasses")

### Python for data & ML (library)
scikit-learn, scipy, statsmodels, xgboost, lightgbm, catboost, pytorch, tensorflow,
keras, jax, transformers (Hugging Face Transformers), mlflow, optuna, langchain,
matplotlib, seaborn, plotly, jupyter (tool), streamlit (framework), opencv, pillow,
nltk, spacy, polars, pyarrow, dask, pyspark, networkx, sympy, numba

### Python libraries & frameworks
requests, httpx, aiohttp, click, flask (framework), django (framework), jinja2,
pydantic, sqlalchemy, celery, pytest, beautifulsoup, scrapy (framework), boto3

### Databases & data (category database unless noted)
database-design (category tool: modelling, normalisation, indexes, transactions, CAP),
postgresql, mysql, sqlite, mongodb, redis, elasticsearch, cassandra, dynamodb,
neo4j, kafka (tool), snowflake, bigquery, duckdb, clickhouse

### Cloud (category cloud)
aws, azure, gcp

### Shell & command line (tool unless noted)
bash (language), zsh, powershell (language), linux, vim, tmux, ssh,
text-processing ("grep, sed & awk"), curl, jq, make, regex, cmake, yaml, markdown,
vscode

### DevOps & infrastructure (tool)
kubernetes, helm, terraform, ansible, github-actions, gitlab-ci, jenkins, nginx,
prometheus, grafana, rabbitmq

### Web (language/framework/tool as fits)
html (language), css (language), tailwind (framework), http ("HTTP & the web", tool),
vue, angular, svelte, nextjs (framework), graphql, grpc, oauth ("OAuth, OIDC & JWT"),
npm, vite, webpack, openapi

### More languages & frameworks
csharp ("C#"), dotnet ("ASP.NET Core", framework), kotlin, swift, dart,
flutter (framework), php, laravel (framework), ruby, rails ("Ruby on Rails",
framework), r ("R"), scala, lua, spring-boot (framework), gradle, maven
