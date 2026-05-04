import { cn } from "@/lib/utils";
import { HTMLAttributes } from "react";

export function Badge({
  className,
  variant = "default",
  ...props
}: HTMLAttributes<HTMLSpanElement> & { variant?: "default" | "secondary" | "destructive" | "outline" }) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium transition-colors",
        {
          "bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-200": variant === "default",
          "bg-gray-100 text-gray-800 dark:bg-gray-700 dark:text-gray-200": variant === "secondary",
          "bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-200": variant === "destructive",
          "border border-gray-300 dark:border-gray-600": variant === "outline",
        },
        className
      )}
      {...props}
    />
  );
}
