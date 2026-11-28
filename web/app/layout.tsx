import type { Metadata } from "next";
import "./globals.css";
export const metadata: Metadata = { title: "CDCI | Drawing Intelligence", description: "Engineering drawing extraction and review workflow" };
export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="en"><body>{children}</body></html>;
}
