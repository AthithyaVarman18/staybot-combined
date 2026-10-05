"""
Local stand-in for Supabase, for development and automated tests only.

It serves the small part of the Supabase REST (PostgREST) and Storage APIs
this app uses, backed by a real local PostgreSQL database, so the whole app
(including database functions, constraints and private file storage) can be
exercised without touching the live Supabase project.

    LOCAL_SUPABASE_DSN="host=127.0.0.1 port=55440 user=postgres dbname=staybot"
    LOCAL_SUPABASE_KEY=local-dev-key
    LOCAL_SUPABASE_FILES=/path/to/private/files
    .venv/bin/uvicorn dev.local_supabase:app --port 54329

Never deploy this. Supported query features: select=<columns>, order, limit,
offset and the filters eq, neq, gt, gte, lt, lte, like, ilike, in, is.
"""

import json
import os
import re
from pathlib import Path

import psycopg
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from psycopg import sql
from psycopg.rows import dict_row

DSN = os.getenv("LOCAL_SUPABASE_DSN", "")
KEY = os.getenv("LOCAL_SUPABASE_KEY", "local-dev-key")
FILES = Path(os.getenv("LOCAL_SUPABASE_FILES", "/tmp/local-supabase-files"))

app = FastAPI(title="Local Supabase stand-in (dev only)")
NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
OPS = {"eq": "=", "neq": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<=", "like": "like", "ilike": "ilike"}


def error(status, code, message):
    return JSONResponse({"code": code, "message": message, "details": None, "hint": None}, status_code=status)


def authorized(request):
    return request.headers.get("apikey") == KEY and request.headers.get("authorization") == f"Bearer {KEY}"


def ident(name):
    if not NAME.match(name):
        raise ValueError(f"bad identifier {name!r}")
    return sql.Identifier(name)


def where_clause(params):
    parts, values = [], []
    for key, raw in params.items():
        if key in ("select", "order", "limit", "offset"):
            continue
        op, _, value = raw.partition(".")
        column = sql.SQL("{}::text").format(ident(key))
        if op in ("like", "ilike"):
            parts.append(sql.SQL("{} {} %s").format(column, sql.SQL(OPS[op])))
            values.append(value.replace("*", "%"))
        elif op in OPS:
            # The untyped parameter takes the column's type (uuid, timestamptz, ...).
            parts.append(sql.SQL("{} {} %s").format(ident(key), sql.SQL(OPS[op])))
            values.append(value.replace("*", "%") if op in ("like", "ilike") else value)
        elif op == "in":
            items = [v.strip().strip('"') for v in value.strip("()").split(",") if v.strip()]
            parts.append(sql.SQL("{} = any(%s)").format(column))
            values.append(items)
        elif op == "is":
            if value not in ("null", "true", "false"):
                raise ValueError("bad is filter")
            parts.append(sql.SQL("{} is " + value).format(ident(key)))
        else:
            raise ValueError(f"unsupported filter {raw!r}")
    if not parts:
        return sql.SQL(""), values
    return sql.SQL(" where ") + sql.SQL(" and ").join(parts), values


def columns(select):
    if not select or select == "*":
        return sql.SQL("*")
    return sql.SQL(", ").join(ident(c.strip()) for c in select.split(","))


def order_clause(order):
    if not order:
        return sql.SQL("")
    items = []
    for item in order.split(","):
        name, *mods = item.split(".")
        direction = "desc" if "desc" in mods else "asc"
        nulls = " nulls first" if "nullsfirst" in mods else " nulls last" if "nullslast" in mods else ""
        items.append(sql.SQL("{} " + direction + nulls).format(ident(name)))
    return sql.SQL(" order by ") + sql.SQL(", ").join(items)


def run(query, values=(), fetch=True):
    with psycopg.connect(DSN, autocommit=True, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(query, values)
            return cur.fetchall() if fetch and cur.description else []


def db_error(exc):
    state = getattr(exc, "sqlstate", None)
    message = str(exc).split("\n")[0]
    if state == "42P01" or state == "42883":
        return error(404, "PGRST205" if state == "42P01" else "PGRST202", message)
    if state == "23505":
        return error(409, state, message)
    return error(400, state or "P0001", message)


def encode(rows):
    return json.loads(json.dumps(rows, default=str))


@app.middleware("http")
async def check_key(request: Request, call_next):
    if not authorized(request):
        return error(401, "401", "Invalid API key")
    return await call_next(request)


@app.get("/rest/v1/{table}")
def select_rows(table: str, request: Request):
    params = dict(request.query_params)
    try:
        where, values = where_clause(params)
        query = sql.SQL("select {} from {}").format(columns(params.get("select")), ident(table)) + where + order_clause(params.get("order"))
        if params.get("limit"):
            query += sql.SQL(" limit {}").format(sql.Literal(int(params["limit"])))
        if params.get("offset"):
            query += sql.SQL(" offset {}").format(sql.Literal(int(params["offset"])))
        return encode(run(query, values))
    except psycopg.Error as exc:
        return db_error(exc)
    except ValueError as exc:
        return error(400, "PGRST100", str(exc))


@app.post("/rest/v1/rpc/{function}")
async def call_function(function: str, request: Request):
    body = await request.json()
    try:
        args = sql.SQL(", ").join(
            sql.SQL("{} => %s").format(ident(k)) for k in body) if body else sql.SQL("")
        values = [None if v is None else json.dumps(v) if isinstance(v, (dict, list)) else str(v).lower() if isinstance(v, bool) else str(v)
                  for v in body.values()]
        retset = run(sql.SQL("select bool_or(proretset) as s from pg_proc where proname = %s"), [function])[0]["s"]
        if retset is None:
            return error(404, "PGRST202", f"function {function} not found")
        rows = run(sql.SQL("select to_jsonb(t) as r from {}({}) t").format(ident(function), args), values)
        result = [r["r"] for r in rows]
        return result if retset else (result[0] if result else None)
    except psycopg.Error as exc:
        return db_error(exc)


@app.post("/rest/v1/{table}")
async def insert_rows(table: str, request: Request):
    body = await request.json()
    records = body if isinstance(body, list) else [body]
    try:
        out = []
        for record in records:
            cols = sql.SQL(", ").join(ident(c) for c in record)
            query = sql.SQL("insert into {t} ({cols}) select {cols} from json_populate_record(null::{t}, %s) returning *").format(
                t=ident(table), cols=cols)
            out += run(query, [json.dumps(record)])
        return JSONResponse(encode(out), status_code=201)
    except psycopg.Error as exc:
        return db_error(exc)


@app.patch("/rest/v1/{table}")
async def update_rows(table: str, request: Request):
    body = await request.json()
    params = dict(request.query_params)
    try:
        where, values = where_clause(params)
        if not any(k not in ("select", "order", "limit", "offset") for k in params):
            return error(400, "21000", "UPDATE requires a WHERE clause")
        assignments = sql.SQL(", ").join(
            sql.SQL("{c} = (json_populate_record(null::{t}, %s)).{c}").format(c=ident(c), t=ident(table)) for c in body)
        query = sql.SQL("update {} set ").format(ident(table)) + assignments + where + sql.SQL(" returning *")
        return encode(run(query, [json.dumps(body)] * len(body) + values))
    except psycopg.Error as exc:
        return db_error(exc)


@app.delete("/rest/v1/{table}")
def delete_rows(table: str, request: Request):
    params = dict(request.query_params)
    try:
        where, values = where_clause(params)
        run(sql.SQL("delete from {}").format(ident(table)) + where, values, fetch=False)
        return Response(status_code=204)
    except psycopg.Error as exc:
        return db_error(exc)


# ---------------------------------------------------------------------
# Storage (private buckets only)
# ---------------------------------------------------------------------

def object_path(bucket, path):
    target = (FILES / bucket / path).resolve()
    if not str(target).startswith(str((FILES / bucket).resolve()) + os.sep):
        raise ValueError("bad path")
    return target


@app.get("/storage/v1/bucket/{bucket}")
def get_bucket(bucket: str):
    if not (FILES / bucket).is_dir():
        return JSONResponse({"statusCode": "404", "error": "Bucket not found", "message": "Bucket not found"}, status_code=400)
    return {"id": bucket, "name": bucket, "public": False}


@app.post("/storage/v1/bucket")
async def create_bucket(request: Request):
    body = await request.json()
    if body.get("public"):
        return JSONResponse({"message": "local stand-in only supports private buckets"}, status_code=400)
    (FILES / body["id"]).mkdir(parents=True, exist_ok=True)
    return {"name": body["id"]}


@app.post("/storage/v1/object/{bucket}/{path:path}")
async def upload_object(bucket: str, path: str, request: Request):
    target = object_path(bucket, path)
    if target.exists() and request.headers.get("x-upsert") != "true":
        return JSONResponse({"statusCode": "409", "error": "Duplicate", "message": "The resource already exists"}, status_code=400)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(await request.body())
    (target.parent / (target.name + ".content-type")).write_text(request.headers.get("content-type", "application/octet-stream"))
    return {"Key": f"{bucket}/{path}"}


@app.get("/storage/v1/object/{bucket}/{path:path}")
def download_object(bucket: str, path: str):
    target = object_path(bucket, path)
    if not target.is_file():
        return JSONResponse({"statusCode": "404", "error": "not_found", "message": "Object not found"}, status_code=400)
    kind = target.parent / (target.name + ".content-type")
    return Response(target.read_bytes(), media_type=kind.read_text() if kind.exists() else "application/octet-stream")
