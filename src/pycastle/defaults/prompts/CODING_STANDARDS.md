Use pytest for all tests. Prefer `pytest.fixture` over setUp/tearDown patterns. Use `ruff` for linting and `mypy` for type checking.

---

Optional parameters passed to functions should be scrutinised extremely carefully. They are a huge source of bugs (by omission). Prioritise correctness over backwards compatibility.

---

## Testing

### Good

```python
# GOOD: Tests observable behavior through the public interface
def test_create_user_makes_user_retrievable():
    user = create_user(name="Alice")
    retrieved = get_user(user.id)
    assert retrieved.name == "Alice"
```

### Bad

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
- File system when a real instance isn't practical
- Databases when a real instance isn't practical — prefer a test DB over a mock

**Never mock your own classes/modules or internal collaborators.** If something is hard to test without mocking internals, redesign the interface.

#### Designing for Mockability

At system boundaries, prefer SDK-style interfaces — one specific function per operation rather than a generic caller:

```python
# GOOD: Each function is independently mockable, returns one specific shape
class EmailClient:
    def send_welcome(self, to: str) -> None: ...
    def send_password_reset(self, to: str, token: str) -> None: ...

# BAD: Mocking requires conditional logic inside the mock
class EmailClient:
    def send(self, template: str, to: str, context: dict) -> None: ...
```

## Interface Design

### Deep Modules

Prefer deep modules: small interface, deep implementation. A few functions with simple params hiding complex logic behind them.

Avoid shallow modules: large interface with many functions that just pass through to thin implementation. When designing, ask: can I reduce the number of functions? Can I simplify the parameters? Can I hide more complexity inside?

### Design for Testability

1. **Accept dependencies, don't create them** — pass external dependencies in rather than constructing them internally.
2. **Return results, don't produce side effects** — a function that returns a value is easier to test than one that mutates state.
3. **Small surface area** — fewer functions = fewer tests needed, fewer params = simpler test setup.
