import type { NextConfig } from "next";
// `*.dev.tsx` pages (component playgrounds under app/dev/) exist only under `next dev`;
// the static export never sees them as routes, so they can't ship to production.
const dev = process.env.NODE_ENV === "development";
const config: NextConfig = {
  output: "export",
  trailingSlash: true,
  images: { unoptimized: true },
  pageExtensions: dev ? ["dev.tsx", "tsx", "ts", "jsx", "js"] : ["tsx", "ts", "jsx", "js"],
};
export default config;
