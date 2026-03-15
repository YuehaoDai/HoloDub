import type { Config } from "tailwindcss";

export default {
  content: ["./index.html", "./src/**/*.{vue,ts,tsx}"],
  theme: {
    extend: {
      colors: {
        sidebar: "#0f1117",
        surface: "#171b26",
        card: "#1e2535",
        border: "#273246",
        muted: "#37465f",
        text: "#f2f5f7",
        subtle: "#9db0c9",
      },
    },
  },
  plugins: [],
} satisfies Config;
