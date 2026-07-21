"""keyword_rules — 关键词过滤机制（临时/域级/全局三层）。

匹配引擎在 Parser 输出后、save_raw 之前执行:
  include: 文档必须包含至少一个关键词 (OR 语义), 否则拒绝
  exclude: 文档包含任一关键词则拒绝 (不存 raw, 返回 keyword-filtered)
  regex:   正则匹配, 命中算 include 通过

优先级合并: 临时 (crawl_url 参数) > 域级 (DB scope=domain:xx) > 全局 (DB scope=global)
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("trawler.keyword_rules")


@dataclass
class KeywordRule:
    """单条关键词规则。"""

    name: str
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    regex: list[str] = field(default_factory=list)
    case_sensitive: bool = False
    match_position: str = "any"  # any / title / body
    enabled: bool = True
    scope: str = "global"  # global / domain:example.com
    notes: str = ""
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items()}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> KeywordRule:
        return cls(
            name=data["name"],
            include=data.get("include", []),
            exclude=data.get("exclude", []),
            regex=data.get("regex", []),
            case_sensitive=bool(data.get("case_sensitive", False)),
            match_position=data.get("match_position", "any"),
            enabled=bool(data.get("enabled", True)),
            scope=data.get("scope", "global"),
            notes=data.get("notes", ""),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
        )


class KeywordMatcher:
    """关键词匹配引擎。无状态, 线程安全 (纯函数)。"""

    @staticmethod
    def match(text: str, rules: list[KeywordRule]) -> tuple[bool, str]:
        """匹配文本。返回 (passed, reason)。

        passed=True: 通过所有 enabled 规则
        passed=False: 被 exclude 命中 / include 全不命中 / regex 失败
        reason: 空字符串 (通过) 或失败原因 (供 audit + 错误返回)
        """
        if not rules or not text:
            return True, ""

        for rule in rules:
            if not rule.enabled:
                continue

            flags = 0 if rule.case_sensitive else re.IGNORECASE
            target = text
            if rule.match_position == "title":
                target = text.split("\n", 1)[0].lstrip("#").strip()
            elif rule.match_position == "body":
                parts = text.split("\n", 1)
                target = parts[1] if len(parts) > 1 else ""

            # exclude 检查 (任一命中 → 拒绝)
            for kw in rule.exclude:
                try:
                    if re.search(re.escape(kw), target, flags):
                        return False, f"excluded_keyword: {kw!r} (rule={rule.name})"
                except re.error:
                    pass

            # include 检查 (至少一个命中 → 通过; 全不命中 → 拒绝)
            if rule.include:
                inc_hit = False
                for kw in rule.include:
                    try:
                        if re.search(re.escape(kw), target, flags):
                            inc_hit = True
                            break
                    except re.error:
                        pass
                if not inc_hit:
                    return False, f"include_not_matched (rule={rule.name})"

            # regex 检查 (命中算 include 通过; 有 regex 但全不命中 → 拒绝)
            if rule.regex:
                rx_hit = False
                for rx in rule.regex:
                    try:
                        if re.search(rx, target, flags):
                            rx_hit = True
                            break
                    except re.error as e:
                        log.warning("Invalid regex in rule %s: %s", rule.name, e)
                if not rx_hit:
                    return False, f"regex_not_matched (rule={rule.name})"

        return True, ""


# ── DB CRUD ────────────────────────────────────────────────────────

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS keyword_rules (
    name            TEXT PRIMARY KEY,
    include_json    TEXT NOT NULL DEFAULT '[]',
    exclude_json    TEXT NOT NULL DEFAULT '[]',
    regex_json      TEXT NOT NULL DEFAULT '[]',
    case_sensitive  INTEGER NOT NULL DEFAULT 0,
    match_position  TEXT NOT NULL DEFAULT 'any',
    enabled         INTEGER NOT NULL DEFAULT 1,
    scope           TEXT NOT NULL DEFAULT 'global',
    notes           TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_kw_scope ON keyword_rules(scope, enabled);
"""


def init_schema(conn: sqlite3.Connection) -> None:
    """建 keyword_rules 表 (幂等)。由 db._migrate 调用。"""
    conn.executescript(_SCHEMA_SQL)


def list_rules(conn: sqlite3.Connection, scope: str = "") -> list[KeywordRule]:
    """列出规则 (可选 scope 过滤)。"""
    if scope:
        rows = conn.execute(
            "SELECT * FROM keyword_rules WHERE scope = ? ORDER BY name",
            (scope,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM keyword_rules ORDER BY name").fetchall()
    return [_row_to_rule(r) for r in rows]


def get_rule(conn: sqlite3.Connection, name: str) -> KeywordRule | None:
    row = conn.execute("SELECT * FROM keyword_rules WHERE name = ?", (name,)).fetchone()
    return _row_to_rule(row) if row else None


def add_rule(conn: sqlite3.Connection, rule: KeywordRule) -> KeywordRule:
    """新增 (name 已存在则报错)。"""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rule.created_at = now
    rule.updated_at = now
    try:
        conn.execute(
            """INSERT INTO keyword_rules
               (name, include_json, exclude_json, regex_json, case_sensitive,
                match_position, enabled, scope, notes, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                rule.name,
                json.dumps(rule.include),
                json.dumps(rule.exclude),
                json.dumps(rule.regex),
                int(rule.case_sensitive),
                rule.match_position,
                int(rule.enabled),
                rule.scope,
                rule.notes,
                now,
                now,
            ),
        )
    except sqlite3.IntegrityError:
        raise ValueError(f"keyword rule already exists: {rule.name}")
    return rule


def update_rule(conn: sqlite3.Connection, name: str, **fields: Any) -> KeywordRule | None:
    """更新 (部分字段)。返回更新后的规则, 不存在返回 None。"""
    existing = get_rule(conn, name)
    if not existing:
        return None
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    updatable = {
        "include", "exclude", "regex", "case_sensitive",
        "match_position", "enabled", "scope", "notes",
    }
    sets: list[str] = []
    vals: list[Any] = []
    for k, v in fields.items():
        if k not in updatable or v is None:
            continue
        if k in ("include", "exclude", "regex"):
            sets.append(f"{k}_json = ?")
            vals.append(json.dumps(v))
        else:
            sets.append(f"{k} = ?")
            vals.append(int(v) if k in ("case_sensitive", "enabled") else v)
    if not sets:
        return existing
    sets.append("updated_at = ?")
    vals.append(now)
    vals.append(name)
    conn.execute(f"UPDATE keyword_rules SET {', '.join(sets)} WHERE name = ?", vals)
    return get_rule(conn, name)


def delete_rule(conn: sqlite3.Connection, name: str) -> bool:
    """删除规则。返回是否成功删除。"""
    cur = conn.execute("DELETE FROM keyword_rules WHERE name = ?", (name,))
    return cur.rowcount > 0


def _row_to_rule(row: sqlite3.Row) -> KeywordRule:
    return KeywordRule(
        name=row["name"],
        include=json.loads(row["include_json"] or "[]"),
        exclude=json.loads(row["exclude_json"] or "[]"),
        regex=json.loads(row["regex_json"] or "[]"),
        case_sensitive=bool(row["case_sensitive"]),
        match_position=row["match_position"],
        enabled=bool(row["enabled"]),
        scope=row["scope"],
        notes=row["notes"] or "",
        created_at=row["created_at"] or "",
        updated_at=row["updated_at"] or "",
    )


# ── 规则加载与合并 ──────────────────────────────────────────────────

def load_rules_for_domain(
    conn: sqlite3.Connection,
    domain: str,
) -> list[KeywordRule]:
    """加载域级 + 全局规则 (enabled only)。临时规则由调用方自行合并。"""
    rules: list[KeywordRule] = []
    # 域级
    rows = conn.execute(
        "SELECT * FROM keyword_rules WHERE scope = ? AND enabled = 1 ORDER BY name",
        (f"domain:{domain}",),
    ).fetchall()
    rules.extend(_row_to_rule(r) for r in rows)
    # 全局
    rows = conn.execute(
        "SELECT * FROM keyword_rules WHERE scope = 'global' AND enabled = 1 ORDER BY name"
    ).fetchall()
    rules.extend(_row_to_rule(r) for r in rows)
    return rules


def make_temporary_rule(
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    regex: list[str] | None = None,
) -> KeywordRule:
    """构造临时规则 (调用级, 不入库)。优先级最高。"""
    return KeywordRule(
        name="__temporary__",
        include=include or [],
        exclude=exclude or [],
        regex=regex or [],
        enabled=True,
        scope="temporary",
    )
