# Interface Design for Testability

Good interfaces make testing natural:

**1. Accept dependencies, don't create them**

```python
# Testable
def process_order(order, payment_gateway):
    return payment_gateway.charge(order.total)

# Hard to test
def process_order(order):
    gateway = StripeGateway()
    return gateway.charge(order.total)
```

**2. Return results, don't produce side effects**

```python
# Testable
def calculate_discount(cart) -> Discount:
    ...

# Hard to test
def apply_discount(cart) -> None:
    cart.total -= discount
```

**3. Small surface area**

- Fewer methods = fewer tests needed
- Fewer parameters = simpler test setup
- Optional parameters that change behavior are a warning sign — consider separate functions instead

When designing a new interface, ask:

- Can I reduce the number of methods or parameters?
- Can I accept a dependency rather than creating it?
- Does every method return a value the caller can assert on?
