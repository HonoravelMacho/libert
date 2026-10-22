"""IPFS: download por race em múltiplos gateways (o primeiro que responder ganha)."""

from __future__ import annotations

import concurrent.futures as cf

import requests

GATEWAYS = [
    "https://ipfs.io/ipfs/{cid}",
    "https://dweb.link/ipfs/{cid}",
    "https://w3s.link/ipfs/{cid}",
    "https://flk-ipfs.xyz/ipfs/{cid}",
    "https://gateway.pinata.cloud/ipfs/{cid}",
    "https://hardbin.com/ipfs/{cid}",
    "https://4everland.io/ipfs/{cid}",
    "https://ipfs.eth.aragon.network/ipfs/{cid}",
    "https://bluelight.ink/ipfs/{cid}",
    "https://trustless-gateway.link/ipfs/{cid}",
]

CHUNK = 1024 * 64


def _looks_like_html(data: bytes) -> bool:
    head = data.lstrip()[:200].lower()
    return head.startswith((b"<!doctype html", b"<html", b"<head", b"<title"))


def _probe(url: str, headers: dict | None, timeout: tuple[int, int]):
    r = requests.get(url, headers=headers or {}, timeout=timeout, stream=True,
                     allow_redirects=True)
    if r.status_code != 200:
        r.close()
        raise ValueError(f"HTTP {r.status_code}")
    ctype = (r.headers.get("Content-Type", "") or "").lower()
    if "text/html" in ctype:
        r.close()
        raise ValueError("gateway devolveu HTML")
    first = next(r.iter_content(CHUNK), b"")
    if not first:
        r.close()
        raise ValueError("sem conteúdo")
    return r, first


def race(cids: list[str], headers: dict | None = None, wall: float = 22.0):
    """Dispara os gateways em paralelo e devolve a primeira resposta válida.

    Retorna (resposta, first_chunk, gateway_url) ou None. Deadline global curto:
    nenhum gateway vale mais de ~1s de espera se o resto falha.
    """
    urls: list[str] = []
    for cid in cids:
        for g in GATEWAYS[:6]:
            urls.append(g.format(cid=cid))
    if len(urls) > 16:
        urls = urls[:16]
    winner = None
    with cf.ThreadPoolExecutor(min(len(urls), 16)) as ex:
        futures = {ex.submit(_probe, u, headers, (8, wall)): u for u in urls}
        try:
            for fut in cf.as_completed(futures, timeout=wall):
                try:
                    r, first = fut.result()
                except Exception:
                    continue
                url = futures[fut]
                winner = (r, first, url)
                break
        except cf.TimeoutError:
            pass
    return winner
