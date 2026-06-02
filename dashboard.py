"""
FastAPI dashboard: login-protected web UI showing the experiment's
equity curve, stats, trades and daily performance.
Run with: uvicorn dashboard:app --host 0.0.0.0 --port 8000
"""
import secrets
import logging
from pathlib import Path

from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from config import (DASHBOARD_USER, DASHBOARD_PASSWORD, DASHBOARD_SECRET)
import database as db

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("dashboard")

BASE_DIR = Path(__file__).parent
app = FastAPI(title="AI Trading Bot Dashboard")
app.add_middleware(SessionMiddleware, secret_key=DASHBOARD_SECRET, max_age=86400)

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.on_event("startup")
async def _startup():
    await db.init_db()


# ── Auth ─────────────────────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@app.post("/login")
async def login_submit(request: Request,
                       username: str = Form(...),
                       password: str = Form(...)):
    ok_user = secrets.compare_digest(username, DASHBOARD_USER)
    ok_pass = secrets.compare_digest(password, DASHBOARD_PASSWORD)
    if ok_user and ok_pass:
        request.session["authenticated"] = True
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        "login.html", {"request": request, "error": "Credenziali non valide"},
        status_code=401,
    )


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ── Pages ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if not request.session.get("authenticated"):
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse("dashboard.html", {"request": request})


# ── JSON API (used by the frontend) ──────────────────────────────────────────

def _guard(request: Request):
    if not request.session.get("authenticated"):
        raise HTTPException(status_code=401, detail="Non autorizzato")


@app.get("/api/stats")
async def api_stats(request: Request):
    _guard(request)
    return JSONResponse(await db.get_stats())


@app.get("/api/equity")
async def api_equity(request: Request):
    _guard(request)
    return JSONResponse(await db.get_equity_curve())


@app.get("/api/trades")
async def api_trades(request: Request):
    _guard(request)
    return JSONResponse(await db.get_trades())


@app.get("/api/daily")
async def api_daily(request: Request):
    _guard(request)
    return JSONResponse(await db.get_daily_performance())
