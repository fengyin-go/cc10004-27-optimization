.PHONY: install backend frontend test test-http

install:
	cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
	cd frontend && npm install

backend:
	cd backend && ./run.sh

frontend:
	cd frontend && npm run dev

# 通知投递处置规则的单元测试（仅依赖标准库 unittest）
test:
	cd backend && .venv/bin/python -m unittest tests.test_delivery -v

# HTTP 端到端冒烟：需先在 8000 端口启动后端（make backend）
test-http:
	cd backend && .venv/bin/python -I tests/http_smoke.py http://127.0.0.1:8000
