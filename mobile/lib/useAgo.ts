// "3s ago" caption that ticks once per second while `lastSeen` is set.
import { useEffect, useState } from 'react';

export function useAgo(lastSeen: number | null): string {
  const [ago, setAgo] = useState('');
  useEffect(() => {
    const f = () => {
      if (!lastSeen) return setAgo('');
      const s = Math.max(0, Math.round((Date.now() - lastSeen) / 1000));
      setAgo(s < 2 ? 'just now' : `${s}s ago`);
    };
    f();
    const t = setInterval(f, 1000);
    return () => clearInterval(t);
  }, [lastSeen]);
  return ago;
}
