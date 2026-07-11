export function parseEmail(value: string): string {
  if (!value.includes("@")) {
    throw new Error("invalid email");
  }
  return value;
}
