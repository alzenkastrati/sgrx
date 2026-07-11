import { validateEmail } from "../src/validation";

if (!validateEmail("reader@example.test")) {
  throw new Error("fixture validation failed");
}
