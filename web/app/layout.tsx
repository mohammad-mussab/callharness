import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: "CallHarness — Call Analytics",
  description: "Open-source call analytics for voice AI agents",
};

const nav = [
  { href: "/", label: "Dashboard" },
  { href: "/calls", label: "Calls" },
  { href: "/gaps", label: "Missing Information" },
  { href: "/disputes", label: "Disputed Calls" },
  { href: "/latency", label: "Latency & Quality" },
  { href: "/alerts", label: "Alerts" },
  { href: "/evaluators", label: "Custom Checks" },
  { href: "/settings", label: "Analysis Settings" },
];

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <div className="flex min-h-screen">
          <aside className="hidden w-56 shrink-0 border-r border-zinc-800 bg-zinc-950 p-4 md:block">
            <Link href="/" className="flex items-center gap-2 px-2 py-1">
              <span className="flex h-7 w-7 items-center justify-center rounded-lg bg-indigo-500 text-sm font-bold text-white">
                C
              </span>
              <span className="text-lg font-semibold text-zinc-100">CallHarness</span>
            </Link>
            <nav className="mt-6 space-y-1">
              {nav.map((item) => (
                <Link
                  key={item.href}
                  href={item.href}
                  className="block rounded-lg px-3 py-2 text-sm text-zinc-400 hover:bg-zinc-900 hover:text-zinc-100"
                >
                  {item.label}
                </Link>
              ))}
            </nav>
            <div className="mt-8 px-3 text-xs text-zinc-600">
              Open-source call analytics
              <br />
              for voice AI agents
            </div>
          </aside>
          <main className="min-w-0 flex-1 p-6">{children}</main>
        </div>
      </body>
    </html>
  );
}
