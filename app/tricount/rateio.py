"""Arredondamento do rateio: distribui centavos sem sobrar nem faltar.

Por que este módulo existe separado do router e do backend:

- **É regra do split, não de transporte.** Decidir quem fica com o centavo que
  não divide é a mesma decisão que a tela de rateio já exibe; não é detalhe da
  API do Tricount.
- **Tem de valer igual para o fake e para o real.** Se o arredondamento morasse
  dentro de `real.py`, o fluxo exercitado em desenvolvimento (`TRICOUNT_PROVIDER=fake`)
  não seria o mesmo que roda em produção, e o bug de centavo só apareceria no ar.
- **É a única parte testável isoladamente.** Função pura, sem banco, sem rede e
  sem `app.*`: só `decimal`. Por isso a verificação de arredondamento da §7 do
  plano consegue rodar num script de algumas linhas, sem subir o app.

O algoritmo é o do **maior resto** (largest remainder): arredonda todo mundo para
baixo e depois devolve os centavos que faltam, um por um, para quem tem a maior
fração descartada.
"""

from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal


def ratear_centavos(valores: list[float]) -> tuple[int, list[int]]:
    """Largest remainder. Devolve (total_centavos, centavos_por_posicao)
    garantindo sum(centavos) == total_centavos.

    Três detalhes que não são arbitrários:

    1. `total_cents` sai do arredondamento **da soma**, não da soma dos
       arredondados. O total da despesa precisa casar exatamente com o total que
       a tela de rateio mostra ao usuário; somar valores já arredondados pode
       errar por um centavo (três cotas de 3,335 arredondadas para cima somam
       10,02, enquanto o total real 10,005 arredonda para 10,01). Aqui o total
       manda e as cotas se ajustam a ele — nunca o contrário.

    2. `Decimal(str(v))` ancora no decimal que o float **imprime**, em vez de na
       expansão binária exata. `10 / 3` é `3.3333333333333335` impresso, mas em
       binário é um valor levemente diferente; `Decimal(3.3333333333333335)`
       traria todo o lixo de base 2 e poderia derrubar um piso para o centavo
       errado (ou somar 0.00000000000001 e empurrar um empate para o lado
       errado). `Decimal(str(v))` trata o número como o humano o leria.

    3. Empate de fração é resolvido pelo **índice** (a chave de ordenação é
       `(-fração, i)`), e a lista de pessoas já chega ordenada por `Pessoa.id`,
       então o mesmo rateio produz sempre o mesmo resultado entre execuções e
       entre processos — importante porque gravamos um `rateio_hash` para
       comparar envios.

    Alocações de 0 centavo são descartadas depois, pelo service: remover zeros
    não quebra a invariante `sum(centavos) == total_centavos`.
    """
    total_cents = int(
        Decimal(str(sum(valores))).scaleb(2).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    )
    exatos = [Decimal(str(v)).scaleb(2) for v in valores]
    pisos = [int(e.to_integral_value(rounding=ROUND_FLOOR)) for e in exatos]
    resto = total_cents - sum(pisos)
    ordem = sorted(range(len(exatos)), key=lambda i: (-(exatos[i] - pisos[i]), i))
    for i in ordem[:resto] if resto > 0 else []:
        pisos[i] += 1
    # Defensivo: com floats, o arredondamento da soma pode em teoria cair abaixo
    # da soma dos pisos. Nesse caso tira centavo de quem tem a menor fração
    # descartada (percorrendo `ordem` de trás para frente), para a invariante
    # valer de todo jeito em vez de o service mandar um total torto para a API.
    for i in reversed(ordem[resto:]) if resto < 0 else []:
        pisos[i] -= 1
    return total_cents, pisos
