Use pytest for all tests. Prefer `pytest.fixture` over setUp/tearDown patterns. Use `ruff` for linting and `mypy` for type checking.

---

Optional parameters passed to functions should be scrutinised extremely carefully. They are a huge source of bugs (by omission). Prioritise correctness over backwards compatibility.

---

## Testing

### Core Principle

Tests verify behavior through public interfaces, not implementation details. Code can change entirely; tests shouldn't break unless behavior changed.

### Good Tests

Integration-style tests that exercise real code paths through public APIs. They describe _what_ the system does, not _how_.

```python
# GOOD: Tests observable behavior through the public interface
def test_create_user_makes_user_retrievable():
    user = create_user(name="Alice")
    retrieved = get_user(user.id)
    assert retrieved.name == "Alice"
```

- Test behavior users/callers care about
- Use the public API only
- Survive internal refactors
- One logical assertion per test

### Bad Tests

```python
# BAD: Mocks internal collaborator, tests HOW not WHAT
def test_checkout_calls_payment_service(mocker):
    mock_payment = mocker.patch("app.payment_service.process")
    checkout(cart, payment)
    mock_payment.assert_called_once_with(cart.total)

# BAD: Bypasses the interface to verify via database
def test_create_user_saves_to_database(db):
    create_user(name="Alice")
    row = db.execute("SELECT * FROM users WHERE name = ?", ("Alice",)).fetchone()
    assert row is not None
```

Red flags:

- Mocking internal collaborators (your own classes/modules)
- Testing private methods (prefixed with `_`)
- Asserting on call counts/order of internal calls
- Test breaks when refactoring without behavior change
- Test name describes HOW not WHAT
- Verifying through external means (e.g. querying a DB) instead of through the interface

### Mocking

Mock at **system boundaries** only:

- External APIs (HTTP calls, email, etc.)
- Time/randomness (`freezegun`, `unittest.mock`)
- File system or databases when a real instance isn't practical

**Never mock your own classes/modules or internal collaborators.** If something is hard to test without mocking internals, redesign the interface.

### TDD Workflow: Vertical Slices

Do NOT write all tests first, then all implementation. That produces tests that verify _imagined_ behavior and are insensitive to real changes.

Correct approach — one test, one implementation, repeat:

```
RED→GREEN: test1→impl1
RED→GREEN: test2→impl2
RED→GREEN: test3→impl3
```

Each test responds to what you learned from the previous cycle. Never refactor while RED — get to GREEN first.

## Interface Design

### Deep Modules

Prefer deep modules: small interface, deep implementation. A few functions with simple params hiding complex logic behind them.

Avoid shallow modules: large interface with many functions that just pass through to thin implementation. When designing, ask: can I reduce the number of functions? Can I simplify the parameters? Can I hide more complexity inside?

### Design for Testability

1. **Accept dependencies, don't create them** — pass external dependencies in rather than constructing them internally.
2. **Return results, don't produce side effects** — a function that returns a value is easier to test than one that mutates state.
3. **Small surface area** — fewer functions = fewer tests needed, fewer params = simpler test setup.
