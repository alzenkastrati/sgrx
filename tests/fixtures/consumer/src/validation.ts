import { z } from "zod";

export const emailSchema = z.string().email();

export function validateEmail(value: string): boolean {
  return emailSchema.safeParse(value).success;
}
