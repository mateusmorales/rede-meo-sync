import "jsr:@supabase/functions-js/edge-runtime.d.ts";
import { createClient } from "jsr:@supabase/supabase-js@2";

const TOKEN = Deno.env.get("ABASTECIMENTO_TOKEN") ?? "";
const PAGINA = 500;

function tokenConfere(a: string, b: string): boolean {
  if (a.length === 0 || a.length !== b.length) return false;
  let dif = 0;
  for (let i = 0; i < a.length; i++) dif |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return dif === 0;
}

const naoExiste = () =>
  new Response("Not Found", { status: 404, headers: { "Content-Type": "text/plain" } });

Deno.serve(async (req: Request) => {
  if (req.method !== "GET") return naoExiste();
  if (TOKEN.length < 32) return naoExiste();

  const url = new URL(req.url);
  const partes = url.pathname.split("/").filter(Boolean);
  const recebido = partes[partes.length - 1] ?? "";
  if (!tokenConfere(recebido, TOKEN)) return naoExiste();

  const sb = createClient(
    Deno.env.get("SUPABASE_URL")!,
    Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
  );

  // recalcula a grade antes de devolver (leva ~15s)
  if (url.searchParams.get("atualizar") === "1") {
    const { error: e1 } = await sb.rpc("abastecimento_grade_refresh");
    if (e1) {
      console.error("falha no refresh:", e1.message);
      return new Response("erro ao atualizar", { status: 500 });
    }
  }

  const { count: esperado, error: eC } = await sb
    .from("abastecimento_grade_cache")
    .select("loja", { count: "exact", head: true });
  if (eC) {
    console.error("falha na contagem:", eC.message);
    return new Response("erro ao contar", { status: 500 });
  }

  const col = ["loja", "categoria", "celula", "faixa", "faixa_ordem",
               "estoque", "venda_90d", "venda_45d_frente", "data_estoque"];

  // o PostgREST corta em 1000 linhas por requisicao — pagina ate o fim
  const todas: Array<Record<string, unknown>> = [];
  for (let ini = 0; ; ini += PAGINA) {
    const { data, error } = await sb
      .from("abastecimento_grade_cache")
      .select(col.join(",") + ",gerado_em")
      .order("loja").order("categoria").order("celula").order("faixa_ordem")
      .range(ini, ini + PAGINA - 1);
    if (error) {
      console.error("falha na leitura:", error.message);
      return new Response("erro ao ler", { status: 500 });
    }
    const bloco = (data ?? []) as Array<Record<string, unknown>>;
    todas.push(...bloco);
    if (bloco.length < PAGINA) break;
    if (todas.length > 50000) break;
  }

  if (todas.length === 0) {
    return new Response("cache vazio — rode abastecimento_grade_refresh()", { status: 503 });
  }

  // TRAVA: nunca entregar arquivo incompleto em silencio
  if (esperado !== null && todas.length !== esperado) {
    console.error(`incompleto: ${todas.length} de ${esperado}`);
    return new Response(`erro: leitura incompleta (${todas.length} de ${esperado})`, { status: 500 });
  }

  const gerado = String(todas[0]["gerado_em"] ?? "");
  const csv = [col.join(",")]
    .concat(todas.map((l) => col.map((c) => l[c] ?? "").join(",")))
    .join("\n");

  return new Response(csv, {
    status: 200,
    headers: {
      "Content-Type": "text/csv; charset=utf-8",
      "Content-Disposition": `attachment; filename="grade-abastecimento.csv"`,
      "Cache-Control": "no-store",
      "X-Robots-Tag": "noindex, nofollow",
      "X-Gerado-Em": gerado,
      "X-Linhas": String(todas.length),
    },
  });
});
