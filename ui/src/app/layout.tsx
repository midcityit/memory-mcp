import type { Metadata } from "next";
import { Inter } from "next/font/google";
import "./globals.css";
import { ThemeProvider } from "@/components/theme-provider";
import { Header } from "@/components/header";
import { SessionProvider } from "next-auth/react";

const inter = Inter({ subsets: ["latin"] });

export const metadata: Metadata = {
  title: "memory-twin",
  description: "Semantic memory management for AI agents",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body className={`${inter.className} bg-gray-50 text-gray-900 dark:bg-gray-950 dark:text-gray-100 min-h-screen`}>
        <SessionProvider>
          <ThemeProvider>
            <Header />
            <main className="mx-auto max-w-7xl px-4 py-6">{children}</main>
          </ThemeProvider>
        </SessionProvider>
      </body>
    </html>
  );
}
