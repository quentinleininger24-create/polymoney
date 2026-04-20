import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Polymoney",
  description: "Autonomous Polymarket betting",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <div className="mx-auto max-w-6xl p-6">
          <header className="mb-6 flex items-center justify-between border-b border-white/10 pb-4">
            <h1 className="text-xl font-semibold tracking-tight">polymoney</h1>
            <span className="text-xs text-white/50">localhost</span>
          </header>
          {children}
        </div>
      </body>
    </html>
  );
}
