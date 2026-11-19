<script lang="ts">
	import { browser } from '$app/environment';
	import { onMount } from 'svelte';
	import {
		getPrinterConfig,
		getPrinterStatus,
		printPrinterLabel,
		setPrinterConfig,
		type LabelSymbology,
		type PrinterStatus
	} from '$lib';
	import { generateRandomBarcode } from '$lib/barcode';
	import ErrorMessage from '$lib/components/ErrorMessage.svelte';
	import StatusMessage from '$lib/components/StatusMessage.svelte';

	let status: PrinterStatus | null = null;
	let feedDots = 18;
	let itemSymbology: LabelSymbology = 'code128';
	let qrModuleDots = 4;
	let printAs: LabelSymbology | 'config' = 'config';
	let code = '';
	let statusError = '';
	let configError = '';
	let printError = '';
	let message = '';
	let isLoading = true;
	let isRefreshing = false;
	let isSaving = false;
	let isPrinting = false;

	async function fetchStatus(): Promise<void> {
		try {
			status = await getPrinterStatus();
			statusError = '';
		} catch (err) {
			statusError = err instanceof Error ? err.message : 'Failed to read printer status.';
		}
	}

	function applyConfig(config: {
		feed_dots: number;
		item_symbology: LabelSymbology;
		qr_module_dots: number;
	}): void {
		feedDots = config.feed_dots;
		itemSymbology = config.item_symbology;
		qrModuleDots = config.qr_module_dots;
	}

	async function fetchConfig(): Promise<void> {
		try {
			const config = await getPrinterConfig();
			applyConfig(config);
			configError = '';
		} catch (err) {
			configError = err instanceof Error ? err.message : 'Failed to read printer config.';
		}
	}

	async function didClickRefresh(): Promise<void> {
		isRefreshing = true;
		await Promise.all([fetchStatus(), fetchConfig()]);
		isRefreshing = false;
	}

	async function didClickSave(): Promise<void> {
		if (Number.isInteger(feedDots) === false || feedDots < 0 || feedDots > 128) {
			configError = 'Feed dots must be a whole number from 0 to 128.';
			return;
		}
		if (Number.isInteger(qrModuleDots) === false || qrModuleDots < 2 || qrModuleDots > 8) {
			configError = 'QR module dots must be a whole number from 2 to 8.';
			return;
		}

		isSaving = true;
		configError = '';
		message = '';

		try {
			const config = await setPrinterConfig({
				feed_dots: feedDots,
				item_symbology: itemSymbology,
				qr_module_dots: qrModuleDots
			});
			applyConfig(config);
			message = `Saved: feed ${feedDots} dots, item labels ${symbologyName(itemSymbology)}, QR module ${qrModuleDots} dots.`;
		} catch (err) {
			configError = err instanceof Error ? err.message : 'Failed to save printer config.';
		} finally {
			isSaving = false;
		}
	}

	async function didClickPrint(): Promise<void> {
		if (code.length === 0) {
			printError = 'Enter a code to print.';
			return;
		}

		isPrinting = true;
		printError = '';
		message = '';

		try {
			const result = await printPrinterLabel(code, printAs === 'config' ? null : printAs);
			if (result.printed === false) {
				printError = 'The printer is turned off, so the label was not printed.';
			} else {
				message = `Printed ${result.barcode} as ${symbologyName(result.symbology)}.`;
			}
		} catch (err) {
			printError = err instanceof Error ? err.message : 'Failed to print label.';
		} finally {
			isPrinting = false;
			await fetchStatus();
		}
	}

	function symbologyName(symbology: LabelSymbology): string {
		return symbology === 'qr' ? 'QR (2D)' : 'Code 128 (1D)';
	}

	function didClickGenerate(): void {
		code = generateRandomBarcode();
		printError = '';
	}

	async function initialLoad(): Promise<void> {
		isLoading = true;
		await Promise.all([fetchStatus(), fetchConfig()]);
		isLoading = false;
	}

	onMount(() => {
		if (browser === true) {
			void initialLoad();
		}
	});
</script>

<svelte:head>
	<title>Printer • Openshelf</title>
</svelte:head>

{#if isLoading === true}
	<p>loading printer data...</p>
{:else}
	<div class="scroll-x">
		<table>
			<thead>
				<tr>
					<th colspan="2">printer status</th>
				</tr>
			</thead>
			<tbody>
				{#if status !== null}
					<tr>
						<td>enabled</td>
						<td class:status-ok={status.enabled} class:status-error={!status.enabled}>
							{status.enabled ? 'yes' : 'no'}
						</td>
					</tr>
					<tr>
						<td>connected</td>
						<td
							class:status-ok={status.connected || status.simulated}
							class:status-error={!status.connected && !status.simulated}
						>
							{status.simulated ? 'simulated' : status.connected ? 'yes' : 'no'}
						</td>
					</tr>
					<tr>
						<td>healthy</td>
						<td class:status-ok={status.healthy} class:status-error={!status.healthy}>
							{status.healthy ? 'yes' : 'no'}
						</td>
					</tr>
					<tr>
						<td>paper</td>
						<td
							class:status-ok={!status.paper_out && !status.paper_near_end}
							class:status-warning={status.paper_near_end && !status.paper_out}
							class:status-error={status.paper_out}
						>
							{status.paper_out ? 'out' : status.paper_near_end ? 'near end' : 'ok'}
						</td>
					</tr>
					<tr>
						<td>cover</td>
						<td class:status-ok={!status.cover_open} class:status-error={status.cover_open}>
							{status.cover_open ? 'open' : 'closed'}
						</td>
					</tr>
					<tr>
						<td>error</td>
						<td class:status-error={status.error !== null}>{status.error ?? '-'}</td>
					</tr>
				{/if}
			</tbody>

			<thead>
				<tr>
					<th colspan="2">label settings</th>
				</tr>
			</thead>
			<tbody>
				<tr>
					<td><label for="feed-dots">feed dots</label></td>
					<td>
						<input id="feed-dots" type="number" min="0" max="128" step="1" bind:value={feedDots} />
						<span class="hint">blank paper fed after each label (8 dots = 1 mm)</span>
					</td>
				</tr>
				<tr>
					<td><label for="item-symbology">item labels</label></td>
					<td>
						<select id="item-symbology" bind:value={itemSymbology}>
							<option value="code128">Code 128 (1D)</option>
							<option value="qr">QR (2D)</option>
						</select>
						<span class="hint">bin and login labels always print Code 128; scanning reads both</span>
					</td>
				</tr>
				<tr>
					<td><label for="qr-module-dots">QR module dots</label></td>
					<td>
						<input
							id="qr-module-dots"
							type="number"
							min="2"
							max="8"
							step="1"
							bind:value={qrModuleDots}
						/>
						<span class="hint">
							12-digit ID ≈ {((25 * qrModuleDots) / 8).toFixed(1)} mm square; lower is smaller but
							harder to scan
						</span>
					</td>
				</tr>
			</tbody>

			<thead>
				<tr>
					<th colspan="2">test label</th>
				</tr>
			</thead>
			<tbody>
				<tr>
					<td><label for="print-code">code</label></td>
					<td>
						<input id="print-code" type="text" bind:value={code} placeholder="012345678901" />
						<button type="button" class="inline-button" on:click={didClickGenerate}>random</button>
					</td>
				</tr>
				<tr>
					<td><label for="print-as">print as</label></td>
					<td>
						<select id="print-as" bind:value={printAs}>
							<option value="config">saved item setting</option>
							<option value="code128">Code 128 (1D)</option>
							<option value="qr">QR (2D)</option>
						</select>
					</td>
				</tr>
			</tbody>
		</table>
	</div>

	<div class="action-buttons">
		<button type="button" on:click={didClickRefresh} disabled={isRefreshing}>refresh</button>
		<button type="button" on:click={didClickSave} disabled={isSaving}>save settings</button>
		<button type="button" on:click={didClickPrint} disabled={isPrinting || code === ''}>
			print label
		</button>
	</div>

	{#if statusError !== ''}
		<ErrorMessage message={statusError} />
	{/if}

	{#if configError !== ''}
		<ErrorMessage message={configError} />
	{/if}

	{#if message !== ''}
		<StatusMessage {message} />
	{/if}

	{#if printError !== ''}
		<ErrorMessage message={printError} />
	{/if}
{/if}

<style>
	input,
	select {
		min-width: 12em;
		padding: 4px;
		border: 1px solid black;
		font-family: inherit;
	}

	input[type='number'] {
		width: 6em;
	}

	.hint {
		margin-left: 0.5em;
		color: #555;
		font-size: 0.85em;
	}

	.inline-button {
		margin-top: 0;
	}

	.action-buttons {
		display: flex;
		flex-wrap: wrap;
		gap: 1.5em;
		margin-top: 1.5em;
	}

	.status-ok {
		color: #007a00;
	}

	.status-warning {
		color: #9a6500;
	}

	.status-error {
		color: #c40000;
	}
</style>
