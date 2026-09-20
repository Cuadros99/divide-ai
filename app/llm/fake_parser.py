import asyncio

from app.llm.base import ItemExtraido, ListaParser, ResultadoExtracao


class FakeParser(ListaParser):
    """Parser sem custo, usado em desenvolvimento (LLM_PROVIDER=fake)."""

    async def parse_images(self, images: list[bytes]) -> ResultadoExtracao:
        await asyncio.sleep(0.5)
        itens = [
            ItemExtraido(nome="Arroz 5kg", preco=28.90),
            ItemExtraido(nome="Feijão carioca 1kg", preco=8.49),
            ItemExtraido(nome="Leite integral 1L", preco=5.49),
            ItemExtraido(nome="Óleo de soja 900ml", preco=6.49),
            ItemExtraido(nome="Café torrado 500g", preco=15.90),
        ]
        return ResultadoExtracao(
            itens=itens, total_nota=sum(i.preco for i in itens), data_compra="18/09/2026"
        )
