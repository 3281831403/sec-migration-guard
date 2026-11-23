"""迁移安全规划的 15 条评测用例，全部只使用内存中的语句描述。"""

from migrate.plan import (
    classify,
    detect_destructive,
    lock_level,
    migration_batch,
    plan_migration,
    rollback_plan,
    split_batches,
    validate_policy,
)

POLICY = {
    "batch_size": 2,
    "lock_timeout_ms": 3000,
    "block_destructive": True,
    "rollback": True,
}


def test_comment_prefixed_drop_is_classified_as_dangerous():
    """缺陷 1：带注释的语句不能被默认成普通查询。"""
    statement = "/* 临时热修 */ DROP TABLE audit_log"
    result = classify(statement)
    assert result["kind"] == "dangerous"
    assert result["statement"] == statement


def test_index_change_has_full_table_lock_level():
    """缺陷 2：索引变更会锁全表，不能被当成低等级操作。"""
    assert lock_level("CREATE INDEX idx_orders_user ON orders (user_id)") == 3
    assert lock_level("SELECT id FROM orders WHERE id = 1") == 0


def test_batches_mark_all_or_nothing_transaction_boundary():
    """缺陷 3：每一批都必须标记事务边界，失败不能留下半批。"""
    statements = [
        "UPDATE orders SET state = paid WHERE id = 1",
        "UPDATE orders SET state = paid WHERE id = 2",
        "UPDATE orders SET state = paid WHERE id = 3",
    ]
    batches = split_batches(statements, 2)
    assert [batch["transaction"] for batch in batches] == [
        "all_or_nothing",
        "all_or_nothing",
    ]
    assert [batch["size"] for batch in batches] == [2, 1]


def test_plan_generates_prechecks_and_blocks_destructive_statement():
    """缺陷 4：计划必须先做前置校验，破坏性语句不能直接执行。"""
    plan = plan_migration(["ALTER TABLE users DROP COLUMN legacy_flag"], POLICY)
    assert plan["prechecks"] == [
        {"check": "policy", "ok": True},
        {"check": "lock_level", "index": 0, "level": 3},
    ]
    assert plan["ok"] is False
    assert plan["errors"] == ["destructive_statement_blocked"]
    assert plan["batches"] == []


def test_rollback_does_not_restore_whole_table():
    """缺陷 5：回滚只能定向撤销本次变更，不能删掉新写入的数据。"""
    plan = plan_migration(["ALTER TABLE orders ADD COLUMN note TEXT"], POLICY)
    plan["executed"] = [0]
    plan["new_rows"] = [{"table": "orders", "id": 9001}]
    result = rollback_plan(plan)
    assert result["mode"] == "targeted"
    assert result["statements"] == ["ALTER TABLE orders DROP COLUMN note"]
    assert result["preserve_rows"] == [{"table": "orders", "id": 9001}]


def test_destructive_detection_catches_drop_column_and_unconditional_update():
    """缺陷 6：破坏性检测不能只覆盖删表。"""
    assert detect_destructive("ALTER TABLE users DROP COLUMN email") is True
    assert detect_destructive("UPDATE accounts SET active = 0") is True


def test_plan_flags_read_after_drop_column_order_violation():
    """缺陷 7：计划必须发现先删列、后读该列的顺序错误。"""
    plan = plan_migration(
        [
            "ALTER TABLE orders DROP COLUMN coupon_code",
            "SELECT coupon_code FROM orders",
        ],
        POLICY,
    )
    assert plan["errors"] == [
        "destructive_statement_blocked",
        "order_violation:1:reads_dropped_column:coupon_code",
    ]


def test_policy_validation_requires_lock_wait_timeout():
    """缺陷 8：策略校验必须检查锁等待上限。"""
    issues = validate_policy(
        {"batch_size": 1, "block_destructive": True, "rollback": True}
    )
    assert issues == ["missing_lock_timeout"]


def test_migration_batch_does_not_share_statements_between_migrations():
    """缺陷 9：后一个迁移不能带上一个迁移的语句。"""
    migrations = [
        {
            "id": "orders",
            "statements": ["ALTER TABLE orders ADD COLUMN note TEXT"],
        },
        {
            "id": "users",
            "statements": ["ALTER TABLE users ADD COLUMN nickname TEXT"],
        },
    ]
    result = migration_batch(migrations, POLICY)
    second_plan = result["migrations"][1]["plan"]
    assert second_plan["statements"] == [
        "ALTER TABLE users ADD COLUMN nickname TEXT"
    ]
    second_batch_statements = [
        statement
        for batch in second_plan["batches"]
        for statement in batch["statements"]
    ]
    assert second_batch_statements == [
        "ALTER TABLE users ADD COLUMN nickname TEXT"
    ]
    assert all("orders" not in statement for statement in second_plan["statements"])


def test_rollback_plan_rejects_repeated_rollback():
    """缺陷 10：已经回滚过的语句不能再次进入回滚计划。"""
    plan = plan_migration(["ALTER TABLE orders ADD COLUMN note TEXT"], POLICY)
    plan["executed"] = []
    assert rollback_plan(plan)["errors"] == ["no_pending_rollback"]
    plan["executed"] = [0]
    plan["rolled_back"] = [0]
    result = rollback_plan(plan)
    assert result["ok"] is False
    assert result["errors"] == ["no_pending_rollback"]
    assert result["statements"] == []


def test_nullable_column_migration_is_executable():
    """补充 1：普通增加可空列的迁移计划可以执行。"""
    plan = plan_migration(["ALTER TABLE users ADD COLUMN nickname TEXT"], POLICY)
    assert validate_policy(POLICY) == []
    assert plan["ok"] is True
    assert plan["errors"] == []
    assert len(plan["batches"]) == 1
    assert plan["batches"][0]["transaction"] == "all_or_nothing"
    assert plan["rollback_entries"][0]["inverse"] == (
        "ALTER TABLE users DROP COLUMN nickname"
    )


def test_destructive_update_is_rejected_by_migration_batch():
    """补充 2：无条件的破坏性更新必须被批量规划拦截。"""
    result = migration_batch(
        [{"id": "unsafe", "statements": ["UPDATE accounts SET active = 0"]}],
        POLICY,
    )
    assert result["ok"] is False
    assert result["migrations"][0]["plan"]["ok"] is False
    assert result["migrations"][0]["plan"]["errors"] == [
        "destructive_statement_blocked"
    ]


def test_split_batches_preserves_order():
    """补充 3：分批后展开必须与原始语句顺序完全一致。"""
    statements = [
        "ALTER TABLE a ADD COLUMN x TEXT",
        "ALTER TABLE b ADD COLUMN y TEXT",
        "ALTER TABLE c ADD COLUMN z TEXT",
        "ALTER TABLE d ADD COLUMN w TEXT",
    ]
    batches = split_batches(statements, 3)
    flattened = [statement for batch in batches for statement in batch["statements"]]
    assert flattened == statements


def test_rollback_only_targets_executed_current_statements():
    """补充 4：回滚只影响本次计划里已经执行且待回滚的变更。"""
    plan = plan_migration(
        [
            "ALTER TABLE orders ADD COLUMN note TEXT",
            "CREATE INDEX idx_orders_note ON orders (note)",
        ],
        POLICY,
    )
    plan["executed"] = [1]
    result = rollback_plan(plan)
    assert result["indices"] == [1]
    assert result["statements"] == ["DROP INDEX idx_orders_note"]
    assert result["mode"] == "targeted"


def test_independent_batch_planning_matches_single_plans():
    """补充 5：批量规划中的每个计划都要与单独规划完全一致。"""
    migrations = [
        {
            "id": "orders",
            "statements": ["ALTER TABLE orders ADD COLUMN note TEXT"],
        },
        {
            "id": "users",
            "statements": ["ALTER TABLE users ADD COLUMN nickname TEXT"],
        },
    ]
    result = migration_batch(migrations, POLICY)
    assert result["migrations"][0]["plan"] == plan_migration(
        migrations[0]["statements"], POLICY
    )
    assert result["migrations"][1]["plan"] == plan_migration(
        migrations[1]["statements"], POLICY
    )
