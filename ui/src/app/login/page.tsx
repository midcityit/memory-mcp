"use client";

import { signIn } from "next-auth/react";
import { Button } from "@/components/ui/button";
import { Brain } from "lucide-react";

export default function LoginPage() {
  return (
    <div className="flex min-h-screen items-center justify-center bg-gray-50 dark:bg-gray-950">
      <div className="w-full max-w-sm space-y-6 rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900 p-8 shadow-lg">
        <div className="flex flex-col items-center gap-2">
          <Brain className="h-12 w-12 text-blue-600 dark:text-blue-400" />
          <h1 className="text-2xl font-bold">memory-twin</h1>
          <p className="text-sm text-gray-500 dark:text-gray-400">Sign in to manage memories</p>
        </div>
        <Button className="w-full" onClick={() => signIn("microsoft-entra-id", { callbackUrl: "/memories" })}>
          Sign in with Microsoft
        </Button>
      </div>
    </div>
  );
}
