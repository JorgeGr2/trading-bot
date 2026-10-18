# Transacciones de prueba

Los ficheros `*.json` son transacciones **reales** de Solana mainnet, descargadas
el 18-oct-2026 en codificación `jsonParsed` con `maxSupportedTransactionVersion: 1`:
las de la Fase 2 con `getTransaction` desde el RPC público, y las de la Fase 3 con
`getTransactionsForAddress` de Helius. Las `fomo_*` las firma y paga la wallet de FOMO
`AgmLJBMDCqWynYnQiPCuj9ewsNNsBJXyzoUhD9LJzN51`, y el usuario es el segundo firmante.

Los valores esperados de los tests se han comprobado a mano a partir de los
balances de token antes y después de cada transacción.

| Fichero | Firma | Qué es |
|---|---|---|
| `fomo_sell_pumpswap_clmm.json` | `2DgeLNS5rf…` | Venta de un token de pump.fun a USDC vía DFlow → PumpSwap → Raydium CLMM, con comisión de FOMO de 0,10 USDC |
| `fomo_buy_pumpfun.json` | `4kpSUfRrg5…` | Compra en la curva de pump.fun pagando 11 USDC; FOMO paga la renta de la nueva cuenta de token |
| `fomo_sell_all_pumpfun.json` | `4giTDTKbnq…` | Venta de toda la posición en la curva de pump.fun |
| `fomo_sell_jupiter.json` | `3LAeervMCU…` | Venta enrutada por Jupiter v6 en lugar de DFlow |
| `fomo_buy_token2022_transfer_fee.json` | `ZzBTGnmcLS…` | Compra de un token Token-2022 con comisión de transferencia del 1 %: el usuario recibe un 1 % menos de lo que envían los pools |
| `fomo_buy_with_route_dust.json` | `Wa1V1kn98D…` | Compra en la que la ruta deja 0,00000001 de un token intermedio en la wallet (polvo que hay que ignorar) |
| `fomo_deposit_not_a_trade.json` | `37BnvwacQK…` | Depósito de USDC en otro programa (`DepositToken`): no es una compraventa |
| `fomo_buy_route_residue_token2022.json` | `jF5CtiSv9P…` | Compra USDC → token intermedio → memecoin. El intermedio entra y sale de la wallet y deja un resto del 0,69 % (Fase 3) |
| `fomo_buy_route_residue_9bb6.json` | `3ybKr3r8iu…` | Igual, con otro intermedio y un resto del 0,89 % (Fase 3) |
| `fomo_buy_fee_account_rent.json` | `4X7ciHuuT6…` | Compra en la que la instrucción `create` nombra a la wallet como pagadora de la cuenta de comisiones de **otra** wallet, aunque los lamports los pone el relayer. La renta no es del usuario (Fase 3) |
| `pumpfun_fee_distribution_transfer_in.json` | `5jeXu29sYU…` | Reparto automático de pump.fun a los poseedores de un token: la wallet recibe tokens sin firmar ni pagar. Es una transferencia, no una compra (Fase 3) |

Las transacciones sintéticas se generan en el código de los tests y se identifican
como tales.
