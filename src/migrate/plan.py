"""数据库迁移计划与回滚安全的核心桩。

这是唯一实现文件，恰好八个桩。所有输入都是内存中的 SQL 语句描述，
不连接数据库，也不执行 SQL。固定契约为容差 0：分类、锁等级、批次边界、
错误码、顺序与回滚范围都必须精确匹配，禁止模糊匹配或只按关键字包含判断。
"""

import hashlib
import json
import re


def classify(statement: str) -> dict:
    """精确分类单条语句，返回至少包含 kind 与 statement 的字典。

    kind 只能是 read、write、ddl、dangerous：
    read：SELECT；
    write：INSERT、REPLACE，以及带 WHERE 的 UPDATE、DELETE；
    ddl：CREATE TABLE、ALTER TABLE ADD COLUMN、CREATE INDEX；
    dangerous：DROP TABLE、ALTER TABLE DROP COLUMN、TRUNCATE，以及无
    WHERE 的 UPDATE、DELETE。

    必须先去掉 -- 行注释与 /* ... */ 块注释，再按首个 SQL 关键字判断；
    带注释的语句不得落入默认的 read。容差为 0，返回值中的 statement 保留
    原始输入。
    """
    raise NotImplementedError


def lock_level(statement: str) -> int:
    """返回 0 到 3 的锁等级，结构变更必须高于普通读写。

    0：只读，如 SELECT；
    1：行级写，如带 WHERE 的 UPDATE、DELETE；
    2：表级结构变更，如 ALTER TABLE ADD COLUMN；
    3：全表阻塞，如 CREATE INDEX、ALTER TABLE DROP COLUMN、DROP TABLE、
       无 WHERE 的 UPDATE、DELETE。

    索引变更不能按普通读处理，必须返回 3。注释剥离要求与 classify 相同，
    容差为 0。
    """
    raise NotImplementedError


def split_batches(statements: list, size: int) -> list:
    """按 size 切分语句，保持原始顺序并标记事务边界。

    每个批次固定为：
    {"index": 0 起的批次号, "size": 本批语句数,
     "statements": 本批语句列表, "transaction": "all_or_nothing"}。
    all_or_nothing 表示执行器必须把整批放进同一事务，任一条失败就整批
    回滚，不能留下半批。最后一批可以不足 size。空输入返回 []。
    size 不是正整数时抛出 ValueError。容差为 0。
    """
    raise NotImplementedError


def plan_migration(statements: list, policy: dict) -> dict:
    """生成迁移计划，只规划、不执行，也不连接数据库。

    固定字段：
    ok：全部前置校验、顺序校验和破坏性拦截通过时为 True；
    errors：固定错误码列表，空列表表示通过；
    prechecks：前置校验项，有语句时不得为空。顺序固定为
        [{"check": "policy", "ok": ...},
         {"check": "lock_level", "index": 0, "level": ...}, ...]；
    batches：ok 为 True 时由 split_batches 生成，否则固定为 []；
    statements：原始语句副本；
    rollback_entries：与输入语句一一对应，每项为
        {"index": i, "statement": 原语句, "inverse": 逆操作或 None}；
        ALTER TABLE t ADD COLUMN c 的 inverse 为
        ALTER TABLE t DROP COLUMN c，CREATE INDEX i ON ... 的 inverse 为
        DROP INDEX i；
    executed：默认 []；rolled_back：默认 []；new_rows：默认 []。

    错误码顺序固定为：先 validate_policy 的问题，再
    destructive_statement_blocked，最后按语句索引报告
    order_violation:<read index>:reads_dropped_column:<column>。
    破坏性拦截已触发时仍要完成顺序校验，不能跳过。
    批次大小取 policy 的 batch_size，缺省为 1。容差为 0。
    """
    raise NotImplementedError


def rollback_plan(plan: dict) -> dict:
    """为已执行部分生成定向回滚计划，绝不整表恢复到备份时间点。

    只处理 executed 中且不在 rolled_back 中的索引；输出按索引倒序。
    返回固定字段：
    ok：存在待回滚条目时为 True，否则为 False；
    errors：无待回滚条目时为 ["no_pending_rollback"]，否则为 []；
    statements：待回滚条目的 inverse 列表；
    indices：对应的原始语句索引；
    mode：固定为 targeted；
    preserve_rows：plan 中 new_rows 的原样副本，迁移后新写入的数据必须保留。

    未执行或已经回滚的语句不得再次回滚，重复回滚必须返回错误。
    不得生成整表备份恢复动作。容差为 0。
    """
    raise NotImplementedError


def detect_destructive(statement: str) -> bool:
    """判断语句是否破坏性，返回精确布尔值。

    DROP TABLE、ALTER TABLE DROP COLUMN、TRUNCATE、无 WHERE 的 UPDATE
    或 DELETE 返回 True；带 WHERE 的 UPDATE 或 DELETE、ADD COLUMN、
    CREATE INDEX、SELECT 返回 False。必须先剥离注释，不能只检查删表。
    容差为 0。
    """
    raise NotImplementedError


def validate_policy(policy: dict) -> list[str]:
    """返回策略缺失项，错误码按固定顺序排列。

    依次检查三项：
    lock_timeout_ms 缺失、非整数或不大于 0：missing_lock_timeout；
    block_destructive 不为 True：missing_destructive_guard；
    rollback 不为 True：missing_rollback。
    三项都满足时返回 []。不做模糊合并，容差为 0。
    """
    raise NotImplementedError


def migration_batch(migrations: list, policy: dict) -> dict:
    """独立规划多个迁移，禁止共享任何批次或语句列表。

    migrations 每项含 id 与 statements。对每一项分别调用 plan_migration，
    后一个迁移不得携带前一个迁移的语句，批次列表也不得复用。
    返回 {"ok": 所有 plan 的 ok 均为 True,
          "migrations": [{"id": 原 id, "plan": 对应 plan_migration 结果}]}，
    顺序与输入一致。每个 plan 必须与单独调用 plan_migration 完全相等。
    容差为 0。
    """
    raise NotImplementedError
