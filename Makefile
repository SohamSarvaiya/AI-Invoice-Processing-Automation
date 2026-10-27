.PHONY: install test samples run up down demo
install:  ; pip install -r requirements-dev.txt
test:     ; python -m pytest -q
samples:  ; python -m scripts.generate_samples
run:      ; cp -n .env.example .env; uvicorn app.main:app --reload
up:       ; cp -n .env.example .env; docker compose up --build -d
down:     ; docker compose down
demo:     ; python -m scripts.demo
