import asyncio
import base64
import json

from openai import OpenAI

from app.config import settings
from app.llm.base import ItemExtraido, ListaParser, ResultadoExtracao

_JSON_SCHEMA = {
    "name": "lista_compras",
    "schema": {
        "type": "object",
        "properties": {
            "itens": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "nome": {"type": "string"},
                        "preco": {"type": "number"},
                    },
                    "required": ["nome", "preco"],
                    "additionalProperties": False,
                },
            },
            "total_nota": {
                "type": ["number", "null"],
                "description": "Valor total impresso na nota/lista, se houver. null se não for possível identificar.",
            },
            "data_compra": {
                "type": ["string", "null"],
                "description": "Data da compra impressa na nota, no formato DD/MM/AAAA. null se não for possível identificar.",
            },
        },
        "required": ["itens", "total_nota", "data_compra"],
        "additionalProperties": False,
    },
}


_TOLERANCIA = 0.01
_MAX_TENTATIVAS = 2


class OpenAIParser(ListaParser):
    def __init__(self) -> None:
        self._client = OpenAI(api_key=settings.openai_api_key)

    async def parse_images(self, images: list[bytes]) -> ResultadoExtracao:
        image_blocks = [
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{base64.b64encode(img).decode('utf-8')}"},
            }
            for img in images
        ]

        instrucao = (
            "Extraia todos os itens e preços desta lista de compras/nota fiscal, na ordem em que "
            "aparecem. Se o mesmo produto aparecer em mais de uma linha (ex: 2 unidades compradas "
            "separadamente), inclua uma linha para cada ocorrência — não junte nem descarte itens "
            "repetidos dentro desta foto. Também extraia o valor total impresso na nota, se houver, "
            "e a data da compra impressa na nota, no formato DD/MM/AAAA. Se a data não estiver "
            "visível ou legível, não tente adivinhar — retorne null."
            if len(images) == 1
            else (
                f"Estas {len(images)} fotos fazem parte da mesma lista de compras/nota fiscal "
                "(ex: fotos em sequência de um recibo longo, ou ângulos/partes diferentes da mesma lista). "
                "Extraia todos os itens considerando as fotos em conjunto, na ordem em que aparecem. "
                "Dentro de cada foto, se o mesmo produto aparecer em mais de uma linha (ex: 2 unidades "
                "compradas separadamente), inclua uma linha para cada ocorrência. Só descarte uma "
                "repetição quando ela for causada por sobreposição entre fotos consecutivas — ou seja, "
                "quando a mesma linha física da nota foi fotografada duas vezes por estar na borda de "
                "duas fotos adjacentes; nesse caso inclua-a apenas uma vez no resultado final. "
                "Também extraia o valor total impresso na nota, se houver, e a data da compra "
                "impressa na nota, no formato DD/MM/AAAA. Se a data não estiver visível ou "
                "legível, não tente adivinhar — retorne null."
            )
        )

        messages = [
            {
                "role": "system",
                "content": (
                    "Você extrai itens e preços de fotos de listas de compras ou "
                    "notas fiscais brasileiras. Responda somente com os itens e "
                    "preços identificados, em reais (formato decimal, ex: 12.50). "
                    "Cada linha impressa na nota é um item separado, mesmo que o "
                    "nome e o preço sejam idênticos aos de uma linha vizinha."
                ),
            },
            {
                "role": "user",
                "content": [{"type": "text", "text": instrucao}, *image_blocks],
            },
        ]

        resultado, raw_content = await self._chamar(messages)

        tentativas = 1
        while (
            tentativas < _MAX_TENTATIVAS
            and resultado.total_nota is not None
            and abs(resultado.total_nota - sum(i.preco for i in resultado.itens)) > _TOLERANCIA
        ):
            soma = sum(i.preco for i in resultado.itens)
            messages.append({"role": "assistant", "content": raw_content})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"A soma dos itens que você extraiu (R$ {soma:.2f}) não bate com o total "
                        f"impresso na nota (R$ {resultado.total_nota:.2f}). Reveja a(s) foto(s) com "
                        "atenção: procure por itens que ficaram faltando, duplicados, ou linhas "
                        "idênticas seguidas que foram indevidamente juntadas em uma só (cada unidade "
                        "comprada é uma linha separada). Devolva a lista corrigida."
                    ),
                }
            )
            resultado, raw_content = await self._chamar(messages)
            tentativas += 1

        return resultado

    async def _chamar(self, messages: list[dict]) -> tuple[ResultadoExtracao, str]:
        response = await asyncio.to_thread(
            self._client.chat.completions.create,
            model=settings.openai_model,
            messages=messages,
            response_format={"type": "json_schema", "json_schema": _JSON_SCHEMA},
        )
        raw_content = response.choices[0].message.content
        data = json.loads(raw_content)
        itens = [ItemExtraido(**item) for item in data["itens"]]
        return (
            ResultadoExtracao(
                itens=itens,
                total_nota=data.get("total_nota"),
                data_compra=data.get("data_compra"),
            ),
            raw_content,
        )
