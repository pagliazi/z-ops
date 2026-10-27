import { Loader2 } from 'lucide-react';

export default function Loading() {
  return (
    <div className="flex-1 flex items-center justify-center">
      <div className="flex flex-col items-center gap-3">
        <Loader2 size={24} className="animate-spin text-blue-500" />
        <span className="text-sm text-gray-400">加载中...</span>
      </div>
    </div>
  );
}
