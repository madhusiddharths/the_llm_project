# The router service (src/router_service.py). CPU only: the student runs in the
# vLLM container, and the router only renders prompts, scores log-probs and
# routes. transformers is here for the tokenizer alone (no torch).
FROM python:3.12-slim

WORKDIR /app
RUN pip install --no-cache-dir \
    "pyyaml>=6.0" "pydantic>=2.7" "transformers>=4.44" "tokenizers>=0.19" \
    "huggingface-hub>=0.24" "langgraph>=0.2" "openai>=1.40"

COPY src/ src/
COPY configs/ configs/
COPY data/catalogs/ data/catalogs/

EXPOSE 8080
ENTRYPOINT ["python", "src/router_service.py"]
