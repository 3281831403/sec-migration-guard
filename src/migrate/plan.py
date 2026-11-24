"""数据库迁移计划与回滚安全的核心实现。

所有输入都是内存中的 SQL 语句描述，不连接数据库，也不执行 SQL。
固定契约为容差 0：分类、锁等级、批次边界、错误码、顺序与回滚范围都必须
精确匹配，禁止模糊匹配或只按关键字包含判断。
"""

import re


# 标识符：双引号 / 反引号包裹，或裸标识符
_IDENT = r'(?:"([^"]+)"|`([^`]+)`|([A-Za-z_][A-Za-z0-9_]*))'


def _strip_comments(statement: str) -> str:
    """剥离 -- 行注释与 /* ... */ 块注释，字符串/引号内的内容保持原样。

    缺陷 1/2/6 的共同基础：带注释的语句必须先看到真正的首个 SQL 关键字，
    不能只按原始字符串前缀判断。
    """
    result = []
    i = 0
    n = len(statement)
    quote = None
    while i < n:
        ch = statement[i]
        if quote is not None:
            result.append(ch)
            # SQL 字符串中用连续两个引号转义
            if ch == quote and i + 1 < n and statement[i + 1] == quote:
                result.append(statement[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"', '`'):
            quote = ch
            result.append(ch)
            i += 1
            continue
        if ch == '-' and i + 1 < n and statement[i + 1] == '-':
            # 行注释：一直剥到行尾，换行保留
            i += 2
            while i < n and statement[i] not in '\r\n':
                i += 1
            continue
        if ch == '/' and i + 1 < n and statement[i + 1] == '*':
            # 块注释：剥到对应的 */
            i += 2
            while i < n and not (
                statement[i] == '*' and i + 1 < n and statement[i + 1] == '/'
            ):
                i += 1
            i += 2
            continue
        result.append(ch)
        i += 1
    return ''.join(result)


def _tokens(statement: str) -> list:
    """提取注释剥离后的大写单词 token，跳过字符串字面量。"""
    cleaned = _strip_comments(statement)
    tokens = []
    i = 0
    n = len(cleaned)
    quote = None
    while i < n:
        ch = cleaned[i]
        if quote is not None:
            if ch == quote:
                if i + 1 < n and cleaned[i + 1] == quote:
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if ch in ("'", '"', '`'):
            quote = ch
            i += 1
            continue
        match = re.match(r'[A-Za-z_][A-Za-z0-9_]*', cleaned[i:])
        if match:
            tokens.append(match.group(0).upper())
            i += match.end()
        else:
            i += 1
    return tokens


def _has_where(statement: str) -> bool:
    """WHERE 子句是否真实存在（注释剥离后、字面量外的独立关键字）。"""
    return 'WHERE' in _tokens(statement)


def classify(statement: str) -> dict:
    """精确分类单条语句，返回至少包含 kind 与 statement 的字典。

    kind 只能是 read、write、ddl、dangerous：
    read：SELECT；
    write：INSERT、REPLACE，以及带 WHERE 的 UPDATE、DELETE；
    ddl：CREATE TABLE、ALTER TABLE ADD COLUMN、CREATE INDEX；
    dangerous：DROP TABLE、ALTER TABLE DROP COLUMN、TRUNCATE，以及无
    WHERE 的 UPDATE、DELETE。

    先剥离注释再按首个 SQL 关键字判断；statement 保留原始输入。
    """
    # 缺陷 1 修复：先剥注释，杜绝带注释语句落入默认 read。
    tokens = _tokens(statement)
    first = tokens[0] if tokens else ''

    if first == 'SELECT':
        kind = 'read'
    elif first in ('INSERT', 'REPLACE'):
        kind = 'write'
    elif first in ('UPDATE', 'DELETE'):
        kind = 'write' if _has_where(statement) else 'dangerous'
    elif first == 'TRUNCATE':
        kind = 'dangerous'
    elif first == 'DROP':
        # DROP TABLE 为 dangerous；DROP INDEX 同为破坏性结构变更。
        kind = 'dangerous'
    elif first == 'ALTER':
        kind = (
            'dangerous' if 'DROP' in tokens and 'COLUMN' in tokens else 'ddl'
        )
    elif first == 'CREATE':
        kind = 'ddl'
    else:
        kind = 'read'

    return {'kind': kind, 'statement': statement}


def lock_level(statement: str) -> int:
    """返回 0 到 3 的锁等级，结构变更必须高于普通读写。

    0：只读，如 SELECT；
    1：行级写，如带 WHERE 的 UPDATE、DELETE；
    2：表级结构变更，如 ALTER TABLE ADD COLUMN；
    3：全表阻塞，如 CREATE INDEX、ALTER TABLE DROP COLUMN、DROP TABLE、
       无 WHERE 的 UPDATE、DELETE。
    """
    # 缺陷 2 修复：索引变更与其他全表阻塞结构变更返回 3，不再按普通读处理。
    tokens = _tokens(statement)
    first = tokens[0] if tokens else ''

    if first == 'SELECT':
        return 0
    if first in ('INSERT', 'REPLACE'):
        return 1
    if first in ('UPDATE', 'DELETE'):
        return 1 if _has_where(statement) else 3
    if first == 'TRUNCATE':
        return 3
    if first == 'DROP':
        return 3
    if first == 'ALTER':
        if 'DROP' in tokens and 'COLUMN' in tokens:
            return 3
        return 2
    if first == 'CREATE':
        if 'INDEX' in tokens:
            return 3
        return 2
    return 3


def split_batches(statements: list, size: int) -> list:
    """按 size 切分语句，保持原始顺序并标记事务边界。

    每个批次固定为：
    {"index": 0 起的批次号, "size": 本批语句数,
     "statements": 本批语句列表, "transaction": "all_or_nothing"}。
    空输入返回 []。size 不是正整数时抛出 ValueError。
    """
    # 缺陷 3 修复：每批显式标记 all_or_nothing 事务边界，失败整批回滚，
    # 不会留下半批；bool 不是合法的正整数。
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise ValueError('size must be a positive integer')

    batches = []
    for index, start in enumerate(range(0, len(statements), size)):
        chunk = statements[start:start + size]
        batches.append(
            {
                'index': index,
                'size': len(chunk),
                'statements': list(chunk),
                'transaction': 'all_or_nothing',
            }
        )
    return batches


def validate_policy(policy: dict) -> list:
    """返回策略缺失项，错误码按固定顺序排列。

    依次检查：
    lock_timeout_ms 缺失、非整数或不大于 0：missing_lock_timeout；
    block_destructive 不为 True：missing_destructive_guard；
    rollback 不为 True：missing_rollback。
    """
    # 缺陷 8 修复：锁等待上限 lock_timeout_ms 是必检项，bool 不算整数。
    issues = []
    timeout = policy.get('lock_timeout_ms')
    if (
        timeout is None
        or isinstance(timeout, bool)
        or not isinstance(timeout, int)
        or timeout <= 0
    ):
        issues.append('missing_lock_timeout')
    if policy.get('block_destructive') is not True:
        issues.append('missing_destructive_guard')
    if policy.get('rollback') is not True:
        issues.append('missing_rollback')
    return issues


def detect_destructive(statement: str) -> bool:
    """判断语句是否破坏性，返回精确布尔值。

    DROP TABLE、ALTER TABLE DROP COLUMN、TRUNCATE、无 WHERE 的 UPDATE
    或 DELETE 返回 True；带 WHERE 的 UPDATE 或 DELETE、ADD COLUMN、
    CREATE INDEX、SELECT 返回 False。
    """
    # 缺陷 6 修复：不再只查删表，删列、TRUNCATE、无条件更新/删除全部覆盖；
    # classify 内部已先剥离注释。
    return classify(statement)['kind'] == 'dangerous'


def _dropped_column(statement: str):
    """从 ALTER TABLE ... DROP COLUMN 语句中解析被删列名，否则返回 None。"""
    match = re.search(
        r'DROP\s+(?:COLUMN\s+)?' + _IDENT,
        _strip_comments(statement),
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    return next(group for group in match.groups() if group is not None)


def _selected_columns(statement: str):
    """解析 SELECT 列表中的列名（剥注释、去引号），* 不在其中。"""
    cleaned = _strip_comments(statement)
    match = re.search(r'SELECT\s+(.*?)\s+FROM', cleaned, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return []
    columns = []
    for part in match.group(1).split(','):
        ident = re.fullmatch(r'\s*(?:' + _IDENT + r')\s*', part)
        if ident:
            columns.append(next(g for g in ident.groups() if g is not None))
    return columns


def _inverse(statement: str):
    """生成单条语句的定向逆操作；无对应逆操作时返回 None。"""
    cleaned = _strip_comments(statement)

    add_column = re.search(
        r'ALTER\s+TABLE\s+' + _IDENT + r'\s+ADD\s+(?:COLUMN\s+)?'
        + r'(' + _IDENT + r')',
        cleaned,
        flags=re.IGNORECASE,
    )
    if add_column:
        table = next(g for g in add_column.groups()[:3] if g is not None)
        column = next(g for g in add_column.groups()[3:] if g is not None)
        return f'ALTER TABLE {table} DROP COLUMN {column}'

    create_index = re.search(
        r'CREATE\s+(?:UNIQUE\s+)?INDEX\s+' + _IDENT,
        cleaned,
        flags=re.IGNORECASE,
    )
    if create_index:
        index = next(g for g in create_index.groups() if g is not None)
        return f'DROP INDEX {index}'

    return None


def plan_migration(statements: list, policy: dict) -> dict:
    """生成迁移计划，只规划、不执行，也不连接数据库。"""
    original = list(statements)

    # 缺陷 4 修复（前置校验）：策略校验 + 每条语句的锁等级前置检查。
    policy_issues = validate_policy(policy)
    prechecks = [{'check': 'policy', 'ok': not policy_issues}]
    for index, statement in enumerate(original):
        prechecks.append(
            {
                'check': 'lock_level',
                'index': index,
                'level': lock_level(statement),
            }
        )

    # 缺陷 6 修复（计划层拦截）：破坏性语句一律拦截，绝不直接执行。
    errors = list(policy_issues)
    blocked = [
        index
        for index, statement in enumerate(original)
        if detect_destructive(statement)
    ]
    if blocked:
        errors.append('destructive_statement_blocked')

    # 缺陷 7 修复（顺序校验）：先删列、后读该列必须报 order_violation。
    # 破坏性拦截触发时仍然完成本校验，不跳过；按读语句索引顺序报告。
    dropped_columns = set()
    for index, statement in enumerate(original):
        if classify(statement)['kind'] == 'dangerous':
            dropped = _dropped_column(statement)
            if dropped is not None:
                dropped_columns.add(dropped)
        elif _tokens(statement) and _tokens(statement)[0] == 'SELECT':
            for column in _selected_columns(statement):
                if column in dropped_columns:
                    errors.append(
                        f'order_violation:{index}:'
                        f'reads_dropped_column:{column}'
                    )

    ok = not errors
    batch_size = policy.get('batch_size', 1)
    # 缺陷 3 复用：通过校验的计划批次同样带事务边界。
    batches = split_batches(original, batch_size) if ok else []

    # 缺陷 5 的规划侧：每条语句记录定向 inverse，而不是整表备份恢复。
    rollback_entries = [
        {
            'index': index,
            'statement': statement,
            'inverse': _inverse(statement),
        }
        for index, statement in enumerate(original)
    ]

    return {
        'ok': ok,
        'errors': errors,
        'prechecks': prechecks,
        'batches': batches,
        'statements': original,
        'rollback_entries': rollback_entries,
        'executed': [],
        'rolled_back': [],
        'new_rows': [],
    }


def rollback_plan(plan: dict) -> dict:
    """为已执行部分生成定向回滚计划，绝不整表恢复到备份时间点。"""
    executed = plan.get('executed', [])
    rolled_back = plan.get('rolled_back', [])
    entries_by_index = {
        entry['index']: entry for entry in plan.get('rollback_entries', [])
    }

    # 缺陷 10 修复：只处理已执行且尚未回滚的索引，重复回滚直接报错。
    pending = [
        index
        for index in executed
        if index not in rolled_back and index in entries_by_index
    ]

    if not pending:
        return {
            'ok': False,
            'errors': ['no_pending_rollback'],
            'statements': [],
            'indices': [],
            'mode': 'targeted',
            # 缺陷 5 修复：新写入数据原样保留，绝不被整表恢复删除。
            'preserve_rows': list(plan.get('new_rows', [])),
        }

    ordered = sorted(pending, reverse=True)
    return {
        'ok': True,
        'errors': [],
        'statements': [entries_by_index[index]['inverse'] for index in ordered],
        'indices': ordered,
        'mode': 'targeted',
        # 定向回滚 + 保留新行，不生成任何整表备份恢复动作。
        'preserve_rows': list(plan.get('new_rows', [])),
    }


def migration_batch(migrations: list, policy: dict) -> dict:
    """独立规划多个迁移，禁止共享任何批次或语句列表。"""
    # 缺陷 9 修复：每个迁移独立调用 plan_migration，输入与结果均不复用，
    # 后一个迁移不可能携带前一个迁移的语句或批次。
    results = []
    for migration in migrations:
        single_plan = plan_migration(list(migration['statements']), policy)
        results.append({'id': migration['id'], 'plan': single_plan})

    return {
        'ok': all(item['plan']['ok'] for item in results),
        'migrations': results,
    }
