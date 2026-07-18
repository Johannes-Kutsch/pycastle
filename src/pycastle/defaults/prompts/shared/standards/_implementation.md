<implementation-standards>
## Good Tests

**Integration-style**: Test through real interfaces, not mocks of internal parts.

```python
# GOOD: Tests observable behavior
def test_user_can_checkout_with_valid_cart():
    cart = create_cart()
    cart.add(product)
    result = checkout(cart, payment_method)
    assert result.status == "confirmed"
```

Characteristics:

- Tests behavior users/callers care about
- Uses public API only
- Survives internal refactors
- Describes WHAT, not HOW
- One logical assertion per test

## Bad Tests

**Implementation-detail tests**: Coupled to internal structure.

```python
# BAD: Tests implementation details
def test_checkout_calls_payment_service_process(mocker):
    mock_payment = mocker.patch("myapp.payment_service")
    checkout(cart, payment)
    mock_payment.process.assert_called_once_with(cart.total)
```

Red flags:

- Mocking internal collaborators (your own classes/modules)
- Testing private methods (prefixed with `_`)
- Asserting on call counts/order of internal calls
- Test breaks when refactoring without behavior change
- Test name describes HOW not WHAT
- Verifying through external means instead of the interface

```python
# BAD: Bypasses interface to verify
def test_create_user_saves_to_database(db):
    create_user(name="Alice")
    row = db.execute("SELECT * FROM users WHERE name = ?", ("Alice",)).fetchone()
    assert row is not None

# GOOD: Verifies through interface
def test_create_user_makes_user_retrievable():
    user = create_user(name="Alice")
    retrieved = get_user(user.id)
    assert retrieved.name == "Alice"
```

**Tautological tests**: Expected value restates the implementation, so the test passes by construction.

```python
# BAD: Expected value is recomputed the way the code computes it
def test_calculate_total_sums_line_items():
    items = [{"price": 10}, {"price": 5}]
    expected = sum(i["price"] for i in items)
    assert calculate_total(items) == expected

# GOOD: Expected value is an independent, known literal
def test_calculate_total_sums_line_items():
    assert calculate_total([{"price": 10}, {"price": 5}]) == 15
```

## Escape hatch

If you feel the urge to test a private method or internal collaborator, **stop**. Write the red test at the public interface for the behavior you're implementing. Get to GREEN with flat code first; extracting private helpers is review-stage refactoring — no new tests needed for the helpers.

## Deterministic & Portable Tests

A test's verdict must be a pure function of the code under test — never of the wall clock, timezone, host OS, filesystem contents, environment variables, execution order, or network. Green-at-author-time but red-later-or-elsewhere is a defect.

- **Time**: the suite runs under an autouse frozen-clock fixture in `tests/conftest.py`; keep it intact. Never assert against a hardcoded absolute datetime that the code under test compares to a real clock read (tz-awareness does not save it) — derive instants relative to `datetime.now()` under the frozen clock, or inject `now=` into the code under test.
- **Filesystem**: touch only `tmp_path`; never depend on pre-existing files or their absence unless the test created that state. Build paths with `pathlib`, never hardcoded separators.
- **OS**: no platform assumptions in assertions (separators, line endings, signals, permission bits, tz database on Windows). If behavior differs per platform, test the seam, not the platform.
- **Order & concurrency**: every test passes alone and in any order; never synchronize on `sleep()` — block on events, joins, or futures.
- **Environment**: hermetic — no real network, no ambient credentials or env vars; verdicts must not depend on the machine's setup.

## pytest preference

Use `pytest.fixture` for shared setup. Prefer fixtures over `setUp`/`tearDown`.

```python
@pytest.fixture
def cart():
    return create_cart()

def test_checkout_with_valid_cart(cart):
    result = checkout(cart, payment_method)
    assert result.status == "confirmed"
```

# When to Mock

Mock at **system boundaries** only:

- External APIs (payment, email, third-party services)
- Databases (sometimes — prefer a real test DB)
- Time/randomness (`datetime.now`, `random`)
- File system (sometimes)
- Subprocess calls to external tools (`git`, `docker`, `gh`)

Don't mock:

- Your own classes/modules
- Internal collaborators
- Anything you control

## Designing for Mockability

At system boundaries, design interfaces that are easy to mock:

**1. Use dependency injection**

Pass external dependencies in rather than creating them internally:

```python
# Easy to mock
def process_payment(order, payment_client):
    return payment_client.charge(order.total)

# Hard to mock
def process_payment(order):
    client = StripeClient(os.environ["STRIPE_KEY"])
    return client.charge(order.total)
```

**2. Prefer SDK-style interfaces over generic callers**

Create specific methods for each external operation instead of one generic method with conditional logic:

```python
# GOOD: Each method is independently mockable
class GitService:
    def get_head_sha(self, repo: Path) -> str: ...
    def create_worktree(self, repo: Path, branch: str) -> None: ...
    def try_merge(self, repo: Path, branch: str) -> bool: ...

# BAD: Mocking requires conditional logic inside the mock
class GitService:
    def run(self, args: list[str]) -> str: ...
```

The SDK approach means:

- Each mock returns one specific shape
- No conditional logic in test setup
- Easier to see which operations a test exercises
- Type safety per operation

## Test fixture pattern

```python
@pytest.fixture
def git_svc():
    svc = MagicMock(spec=GitService)
    svc.get_head_sha.return_value = "abc123"
    svc.try_merge.return_value = True
    return svc
```
</implementation-standards>
