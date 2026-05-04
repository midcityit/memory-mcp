export const runtime = "edge";

import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "Memory Twin",
  description: "Browse and search AI agent memories",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body style={{ fontFamily: "system-ui, sans-serif", margin: 0, padding: "1rem", maxWidth: "960px", marginInline: "auto" }}>
        {children}
      </body>
    </html>
  );
}
