# Good and Bad Tests

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

- Tests behavior callers care about
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

## Escape hatch

If you feel the urge to test a private method or internal collaborator, **stop**. Write the red test at the public interface for the behavior you're implementing. Get to GREEN with flat code first, then extract private helpers during refactor — no new tests needed for the helpers.
