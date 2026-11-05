'use client';

import { useRef, useEffect, useState } from 'react';
import { motion } from 'framer-motion';
import { 
  Globe, MessageSquare, Brain, HelpCircle, 
  Server, Network, Cloud, Code, Database, BookOpen,
  Cpu
} from 'lucide-react';

interface Agent {
  status: 'online' | 'offline';
  skills?: string[];
}

interface AgentMeta {
  icon: string;
  color: string;
  label: string;
  desc: string;
}

interface ArchitectureViewProps {
  agents: Record<string, Agent>;
  meta: Record<string, AgentMeta>;
  expandedAgent: string | null;
  setExpandedAgent: (agent: string | null) => void;
  onlineCount: number;
  totalSkills: number;
}

const allAgents = [
  'general_agent', 'ops_agent', 'network_agent', 'vm_agent', 
  'dev_agent', 'browser_agent', 'opscore_agent', 'kb_agent'
];

const agentToServices: Record<string, string[]> = {
  general_agent: ['llm', 'kb'],
  ops_agent: ['llm', 'opscore1', 'opscore2'],
  network_agent: ['llm', 'opscore1'],
  vm_agent: ['llm', 'vmware', 'opscore2'],
  dev_agent: ['llm'],
  browser_agent: ['llm'],
  opscore_agent: ['llm', 'opscore1', 'opscore2'],
  kb_agent: ['llm', 'kb']
};

const services = [
  { id: 'llm', icon: Cpu, label: 'LLM API', color: '#10b981' },
  { id: 'kb', icon: BookOpen, label: '知识库', color: '#8b5cf6' },
  { id: 'opscore1', icon: Server, label: 'OPS-CORE信息', color: '#3b82f6' },
  { id: 'opscore2', icon: Database, label: 'OPS-CORE资源', color: '#6366f1' },
  { id: 'vmware', icon: Cloud, label: 'VMware', color: '#f97316' },
];

const iconMap: Record<string, React.ElementType> = {
  'ri-question-answer-line': HelpCircle,
  'ri-server-line': Server,
  'ri-global-line': Network,
  'ri-cloud-line': Cloud,
  'ri-code-s-slash-line': Code,
  'ri-chrome-line': Globe,
  'ri-database-2-line': Database,
  'ri-book-open-line': BookOpen,
  'ri-brain-line': Brain,
};

interface Line {
  id: string;
  x1: number;
  y1: number;
  x2: number;
  y2: number;
  color: string;
  isOnline: boolean;
}

export function ArchitectureView({ 
  agents, 
  meta, 
  expandedAgent, 
  setExpandedAgent, 
  onlineCount, 
  totalSkills 
}: ArchitectureViewProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const orchRef = useRef<HTMLDivElement>(null);
  const agentRefs = useRef<Record<string, HTMLDivElement | null>>({});
  const serviceRefs = useRef<Record<string, HTMLDivElement | null>>({});
  const [lines, setLines] = useState<Line[]>([]);

  useEffect(() => {
    const calcLines = () => {
      if (!containerRef.current || !orchRef.current) return;

      const containerRect = containerRef.current.getBoundingClientRect();
      const orchRect = orchRef.current.getBoundingClientRect();

      const orchX = orchRect.left + orchRect.width / 2 - containerRect.left;
      const orchY = orchRect.bottom - containerRect.top;

      const newLines: Line[] = [];

      allAgents.forEach((name) => {
        const agentEl = agentRefs.current[name];
        if (agentEl) {
          const agentRect = agentEl.getBoundingClientRect();
          const agentX = agentRect.left + agentRect.width / 2 - containerRect.left;
          const agentY = agentRect.top - containerRect.top;
          const isOnline = agents[name]?.status === 'online';

          newLines.push({
            id: `orch-${name}`,
            x1: orchX, y1: orchY,
            x2: agentX, y2: agentY,
            color: '#8b5cf6',
            isOnline
          });

          const agentServices = agentToServices[name] || [];
          agentServices.forEach(svcId => {
            const svcEl = serviceRefs.current[svcId];
            if (svcEl) {
              const svcRect = svcEl.getBoundingClientRect();
              const svcX = svcRect.left + svcRect.width / 2 - containerRect.left;
              const svcY = svcRect.top - containerRect.top;
              const svc = services.find(s => s.id === svcId);

              newLines.push({
                id: `${name}-${svcId}`,
                x1: agentX, y1: agentRect.bottom - containerRect.top,
                x2: svcX, y2: svcY,
                color: svc?.color || '#9ca3af',
                isOnline: true
              });
            }
          });
        }
      });

      setLines(newLines);
    };

    calcLines();
    window.addEventListener('resize', calcLines);
    return () => window.removeEventListener('resize', calcLines);
  }, [agents]);

  const getIcon = (iconName: string) => {
    const Icon = iconMap[iconName] || HelpCircle;
    return <Icon size={16} />;
  };

  return (
    <div ref={containerRef} className="h-full flex flex-col p-6 bg-gray-50 relative">
      {/* SVG Lines */}
      <svg className="absolute top-0 left-0 w-full h-full pointer-events-none" style={{ zIndex: 1 }}>
        {lines.map(line => (
          <path
            key={line.id}
            d={`M${line.x1},${line.y1} Q${line.x1},${(line.y1 + line.y2) / 2} ${line.x2},${line.y2}`}
            fill="none"
            stroke={line.color}
            strokeWidth="2"
            opacity={line.isOnline ? 0.7 : 0.3}
            strokeDasharray={line.isOnline ? "0" : "4,4"}
          />
        ))}
      </svg>

      {/* Entry Points */}
      <div className="flex justify-center gap-20 mb-4 relative z-10">
        <motion.div 
          initial={{ scale: 0.8, opacity: 0 }}
          animate={{ scale: 1, opacity: 1 }}
          className="flex flex-col items-center"
        >
          <div className="w-14 h-14 rounded-xl flex items-center justify-center shadow-sm bg-blue-500">
            <Globe className="text-white" size={24} />
          </div>
          <div className="mt-2 font-bold text-gray-800 text-sm">Web</div>
          <div className="text-xs text-gray-600">控制台</div>
        </motion.div>
        
        <motion.div 
          initial={{ scale: 0.8, opacity: 0 }}
          animate={{ scale: 1, opacity: 1 }}
          transition={{ delay: 0.1 }}
          className="flex flex-col items-center"
        >
          <div className="w-14 h-14 rounded-xl flex items-center justify-center shadow-sm bg-green-500">
            <MessageSquare className="text-white" size={24} />
          </div>
          <div className="mt-2 font-bold text-gray-800 text-sm">飞书</div>
          <div className="text-xs text-gray-600">机器人</div>
        </motion.div>
      </div>

      {/* Entry to Orchestrator Lines */}
      <div className="flex justify-center h-16 relative z-0">
        <svg width="300" height="64" className="overflow-visible">
          <path d="M75,0 Q75,32 150,64" fill="none" stroke="#3b82f6" strokeWidth="2" opacity="0.6"/>
          <path d="M225,0 Q225,32 150,64" fill="none" stroke="#10b981" strokeWidth="2" opacity="0.6"/>
        </svg>
      </div>

      {/* Orchestrator */}
      <div className="flex justify-center mb-8 relative z-10">
        <motion.div
          ref={orchRef}
          whileHover={{ scale: 1.05 }}
          className="w-40 p-4 rounded-2xl border-2 border-purple-500 bg-purple-50 shadow-sm flex flex-col items-center cursor-pointer"
          onClick={() => setExpandedAgent(expandedAgent === 'orchestrator' ? null : 'orchestrator')}
        >
          <div className="w-14 h-14 rounded-xl bg-purple-200 flex items-center justify-center mb-2">
            <Brain className="text-purple-600" size={32} />
          </div>
          <div className="font-bold text-base text-purple-700">Orchestrator</div>
          <div className="text-xs text-gray-500">智能调度中枢</div>
        </motion.div>
      </div>

      {/* Agents */}
      <div className="mb-12 relative z-10">
        <div className="flex justify-center gap-3 px-4">
          {allAgents.map((name, index) => {
            const m = meta[name];
            const isOnline = agents[name]?.status === 'online';
            return (
              <motion.div
                key={name}
                ref={el => { agentRefs.current[name] = el; }}
                initial={{ y: 20, opacity: 0 }}
                animate={{ y: 0, opacity: 1 }}
                transition={{ delay: index * 0.05 }}
                className="flex flex-col items-center"
                style={{ width: '11%' }}
              >
                <div
                  className={`w-full p-2 rounded-lg border-2 cursor-pointer transition-all hover:shadow-md flex flex-col items-center ${
                    expandedAgent === name ? 'shadow-md' : ''
                  } ${!isOnline ? 'opacity-60' : ''}`}
                  onClick={() => setExpandedAgent(expandedAgent === name ? null : name)}
                  style={{
                    borderColor: expandedAgent === name ? m.color : '#e5e7eb',
                    backgroundColor: expandedAgent === name ? `${m.color}10` : 'white'
                  }}
                >
                  <div 
                    className="w-8 h-8 rounded-md flex items-center justify-center mb-1"
                    style={{ backgroundColor: `${m.color}20`, color: m.color }}
                  >
                    {getIcon(m.icon)}
                  </div>
                  <div className="font-medium text-[10px] text-gray-800 text-center">{m.label}</div>
                  <div className={`mt-1 w-1.5 h-1.5 rounded-full ${isOnline ? 'bg-green-500' : 'bg-gray-300'}`} />
                </div>
              </motion.div>
            );
          })}
        </div>
      </div>

      {/* Services */}
      <div className="mt-auto relative z-10">
        <div className="flex items-center justify-center gap-2 mb-4">
          <div className="h-px bg-gray-300 flex-1 max-w-32"></div>
          <span className="text-xs text-gray-500 font-medium">
            <Cloud size={12} className="inline mr-1" />外部服务 & 数据源
          </span>
          <div className="h-px bg-gray-300 flex-1 max-w-32"></div>
        </div>

        <div className="flex justify-center gap-8 px-4">
          {services.map((svc) => {
            const Icon = svc.icon;
            return (
              <motion.div
                key={svc.id}
                ref={el => { serviceRefs.current[svc.id] = el; }}
                whileHover={{ scale: 1.1 }}
                className="flex flex-col items-center"
                style={{ width: '15%' }}
              >
                <div className="relative">
                  <div 
                    className="w-12 h-12 rounded-xl flex items-center justify-center shadow-sm"
                    style={{ backgroundColor: `${svc.color}15` }}
                  >
                    <Icon size={20} style={{ color: svc.color }} />
                  </div>
                  <div className="absolute -top-1 -right-1 w-3 h-3 rounded-full border-2 border-white bg-green-500"></div>
                </div>
                <span className="text-xs text-gray-600 mt-2 font-medium">{svc.label}</span>
              </motion.div>
            );
          })}
        </div>
      </div>

      {/* Status Panel */}
      <div className="absolute top-4 right-4 bg-white rounded-xl shadow-md border border-gray-200 p-3 w-36 z-20">
        <div className="text-xs font-semibold text-gray-700 mb-2 flex items-center gap-1">
          <span className="w-2 h-2 rounded-full bg-green-500 animate-pulse" /> 系统状态
        </div>
        <div className="space-y-1.5 text-xs">
          <div className="flex justify-between">
            <span className="text-gray-500">Agent</span>
            <span className="font-bold text-green-600">{onlineCount}/{Object.keys(agents).length}</span>
          </div>
          <div className="flex justify-between">
            <span className="text-gray-500">Skills</span>
            <span className="font-bold text-blue-600">{totalSkills}</span>
          </div>
        </div>
      </div>
    </div>
  );
}
