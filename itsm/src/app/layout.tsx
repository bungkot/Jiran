import type { ReactNode } from "react";
import "./globals.css";
import { PROMETHEUS_URL } from "@/lib/prometheus";

export const metadata = {
  title: "ITSM — Monitoring Dashboard",
  description: "Dashboard monitoring sendiri, dibangun dari Next.js.",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="id">
      <body>
        <header>
          <h1>ITSM</h1>
          <p>
            Monitoring dashboard milik sendiri · dari Next.js · sumber data:
            Prometheus
          </p>
        </header>
        <main>{children}</main>
        <footer>API metrik: {PROMETHEUS_URL}</footer>
      </body>
    </html>
  );
}
