'use client';

import { useEffect, useState } from 'react';

import { Shell } from '@/components/shell';
import { Badge, Button, Card, Empty, ErrorText, Field, PageHeader, Table, inputClass } from '@/components/ui';
import { ApiError, api } from '@/lib/api';
import { useApi } from '@/lib/hooks';

interface Kb {
  id: string;
  name: string;
  description: string | null;
  documents: number;
  chunks: number;
  created_at: string;
}
interface Doc {
  id: string;
  filename: string;
  chunk_count: number;
  status: string;
  created_at: string;
}

export default function KnowledgePage() {
  const bases = useApi<Kb[]>('/knowledge', 20_000);
  const [name, setName] = useState('');
  const [selected, setSelected] = useState<string>('');
  const [docs, setDocs] = useState<Doc[]>([]);
  const [title, setTitle] = useState('');
  const [body, setBody] = useState('');
  const [testQ, setTestQ] = useState('');
  const [testRes, setTestRes] = useState<string[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!selected && bases.data && bases.data.length > 0) setSelected(bases.data[0].id);
  }, [bases.data, selected]);

  async function loadDocs(id: string) {
    if (!id) return;
    try {
      setDocs(await api.get<Doc[]>(`/knowledge/${id}/documents`));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    }
  }
  useEffect(() => {
    void loadDocs(selected);
    setTestRes(null);
  }, [selected]);

  async function createBase() {
    if (!name.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await api.post('/knowledge', { name: name.trim() });
      setName('');
      await bases.reload();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function addDoc() {
    if (!title.trim() || !body.trim() || !selected) return;
    setBusy(true);
    setError(null);
    try {
      await api.post(`/knowledge/${selected}/documents`, { title: title.trim(), body });
      setTitle('');
      setBody('');
      await loadDocs(selected);
      await bases.reload();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function delDoc(id: string) {
    try {
      await api.delete(`/knowledge/document/${id}`);
      await loadDocs(selected);
      await bases.reload();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    }
  }

  async function delBase(id: string) {
    try {
      await api.delete(`/knowledge/${id}`);
      if (selected === id) setSelected('');
      await bases.reload();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    }
  }

  async function test() {
    if (!testQ.trim() || !selected) return;
    try {
      setTestRes(await api.get<string[]>(`/knowledge/${selected}/search?q=${encodeURIComponent(testQ.trim())}`));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    }
  }

  return (
    <Shell>
      <PageHeader
        title="База знаний"
        subtitle="Материалы, по которым ИИ отвечает точно: цены, услуги, FAQ. Привяжите базу к сценарию в разделе «Сценарии»"
      />
      <ErrorText>{error ?? bases.error}</ErrorText>

      <div className="grid gap-4 lg:grid-cols-[320px_1fr]">
        <Card title="Базы">
          <div className="mb-3 flex gap-2">
            <input
              className={inputClass}
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Название базы"
            />
            <Button onClick={createBase} disabled={busy || !name.trim()}>
              +
            </Button>
          </div>
          {(bases.data?.length ?? 0) === 0 ? (
            <Empty>Баз пока нет</Empty>
          ) : (
            <ul className="space-y-1">
              {bases.data?.map((kb) => (
                <li key={kb.id}>
                  <button
                    onClick={() => setSelected(kb.id)}
                    className={`flex w-full items-center justify-between rounded-lg px-3 py-2 text-left text-sm transition ${
                      selected === kb.id ? 'bg-accent-soft text-slate-100' : 'text-slate-400 hover:bg-ink-800'
                    }`}
                  >
                    <span className="truncate">{kb.name}</span>
                    <span className="ml-2 shrink-0 text-xs text-slate-500">
                      {kb.documents} док · {kb.chunks} фрагм.
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
          <p className="mt-3 text-xs text-slate-500">id базы для сценария копируется из адреса/списка.</p>
        </Card>

        <div className="space-y-4">
          {selected && (
            <>
              <Card
                title="Добавить материал"
                actions={
                  <Button variant="danger" onClick={() => delBase(selected)}>
                    Удалить базу
                  </Button>
                }
              >
                <div className="mb-2 text-xs text-slate-500">id: {selected}</div>
                <Field label="Заголовок">
                  <input className={inputClass} value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Тарифы / FAQ / О психологе" />
                </Field>
                <div className="mt-3">
                  <Field label="Текст" hint="Вставьте материал — он порежется на фрагменты и проиндексируется">
                    <textarea
                      className={`${inputClass} h-40 resize-y`}
                      value={body}
                      onChange={(e) => setBody(e.target.value)}
                      placeholder="Цена терапии 3500, первая консультация 4000. Принимает онлайн и очно в Москве…"
                    />
                  </Field>
                </div>
                <div className="mt-3">
                  <Button onClick={addDoc} disabled={busy || !title.trim() || !body.trim()}>
                    {busy ? 'Индексируем…' : 'Добавить в базу'}
                  </Button>
                </div>
              </Card>

              <Card title="Проверить поиск">
                <div className="flex gap-2">
                  <input className={inputClass} value={testQ} onChange={(e) => setTestQ(e.target.value)} placeholder="сколько стоит терапия?" />
                  <Button variant="ghost" onClick={test} disabled={!testQ.trim()}>
                    Найти
                  </Button>
                </div>
                {testRes && (
                  <div className="mt-3 space-y-2">
                    {testRes.length === 0 ? (
                      <Empty>Ничего не нашлось — добавьте материал по теме</Empty>
                    ) : (
                      testRes.map((r, i) => (
                        <div key={i} className="rounded-lg border border-ink-800 bg-ink-900/40 px-3 py-2 text-xs text-slate-300">
                          {r}
                        </div>
                      ))
                    )}
                  </div>
                )}
              </Card>

              <Card title="Материалы базы">
                {docs.length === 0 ? (
                  <Empty>В базе пока пусто</Empty>
                ) : (
                  <Table head={['Заголовок', 'Фрагментов', 'Статус', '']}>
                    {docs.map((d) => (
                      <tr key={d.id} className="border-b border-ink-800/70 last:border-0">
                        <td className="py-3 pr-4 text-slate-200">{d.filename}</td>
                        <td className="py-3 pr-4 text-slate-400">{d.chunk_count}</td>
                        <td className="py-3 pr-4">
                          <Badge tone={d.status === 'READY' ? 'ok' : 'mute'}>{d.status}</Badge>
                        </td>
                        <td className="py-3">
                          <Button variant="danger" onClick={() => delDoc(d.id)}>
                            Удалить
                          </Button>
                        </td>
                      </tr>
                    ))}
                  </Table>
                )}
              </Card>
            </>
          )}
        </div>
      </div>
    </Shell>
  );
}
