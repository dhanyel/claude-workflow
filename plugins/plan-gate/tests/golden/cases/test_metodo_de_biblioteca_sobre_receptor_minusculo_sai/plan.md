# Plano

### Task 1: fazer

**Files:**
- Create: `src/a.ts`

```typescript
test('x', async ({ page }) => {
  const card = page.getByTestId('remessa');
  await expect(card.getByRole('button')).toBeVisible();
});
```
