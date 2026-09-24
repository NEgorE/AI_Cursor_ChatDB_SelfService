"""Страница входа (код из бота), выход."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from app.web.auth import (
    SESSION_COOKIE,
    SESSION_TTL_SECONDS,
    consume_login_code,
    encode_session,
)
from app.web.deps import load_actor_optional, render

router = APIRouter()


@router.get("/login")
async def login_page(request: Request):
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        if load_actor_optional(request, session) is not None:
            return RedirectResponse("/", status_code=303)
    return render(request, "login.html")


@router.post("/login")
async def login(request: Request):
    form = await request.form()
    raw_code = str(form.get("code", ""))
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        user = consume_login_code(session, raw_code)
        if user is None:
            return render(
                request,
                "login.html",
                error=(
                    "Неверный или устаревший код. Запросите новый в боте: /weblogin"
                ),
                status_code=401,
            )
        user_id = user.id
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        encode_session(request.app.state.signer, user_id),
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
    )
    return response


@router.post("/logout")
async def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response
