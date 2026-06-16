/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,jsx}"],
  theme: {
    extend: {
      colors: {
        paper: "#FBFAF7",
        ink: "#1A2420",
        line: "#E7E4DC",
        basil: {
          DEFAULT: "#3C7A57",
          dark: "#2E5E43",
          soft: "#EAF2EC",
        },
        have: "#4F9D69",
        miss: "#C8772E",
      },
      fontFamily: {
        display: ['"Bricolage Grotesque Variable"', "system-ui", "sans-serif"],
        body: ['"Inter Variable"', "system-ui", "sans-serif"],
      },
      boxShadow: {
        card: "0 1px 2px rgba(26,36,32,0.05), 0 8px 24px -16px rgba(26,36,32,0.25)",
      },
      borderRadius: { xl2: "1.1rem" },
    },
  },
  plugins: [],
};
